"""Matched 20B legacy/balanced replay; fixture tools remain inert assessment data."""

import argparse
import copy
import json
import os
import secrets
from contextlib import contextmanager
from pathlib import Path

from tools.benchmark.client import IntarisClient
from tools.benchmark.cognis_direct import DirectProxy
from tools.benchmark.full_release import SourceHooks, planned_jobs, write_json
from tools.benchmark.matched import Runtime
from tools.benchmark.release_proxy import load_previous_completion
from tools.benchmark.source_history_runner import PublicHistoryLifecycle
from tools.benchmark.source_local_factory import LocalFactory


class ModeClient(IntarisClient):
    review_timeout_s = 600
    last_evaluation = None

    def __init__(self, *args, mode, evidence_path, **kwargs):
        super().__init__(*args, **kwargs)
        self.mode, self.evidence_path = mode, evidence_path

    def create_session(self, session_id, intention, **kwargs):
        policy = dict(kwargs.get("policy") or {})
        policy["evaluation_policy_version"] = self.mode
        if self.mode == "hybrid_v1":
            policy.update(profile="balanced", interaction_mode="step_requests")
        kwargs["policy"] = policy
        result = super().create_session(session_id, intention, **kwargs)
        observed = self.get_session(session_id)
        with self.evidence_path.open("a") as stream:
            stream.write(json.dumps({"mode": self.mode, "session": observed}) + "\n")
        effective = observed.get("policy") or {}
        if effective.get("evaluation_policy_version", "legacy") != self.mode:
            raise ValueError("Effective session mode differs from requested mode")
        if self.mode == "hybrid_v1":
            if effective.get("active_policy", {}).get("profile") != "balanced":
                raise ValueError("Effective profile is not balanced")
            if effective.get("interaction_mode") != "step_requests":
                raise ValueError("Effective interaction mode is not step_requests")
        return result

    def evaluate(self, *args, **kwargs):
        checkpoint = getattr(self, "_human_checkpoint", None)
        if checkpoint:
            session_id, action = checkpoint
            if args[:3] != (session_id, action["tool"], action["args"]):
                raise ValueError("Historical checkpoint cannot apply to another action")
            kwargs["minimum_outcome"] = "escalate"
        result = super().evaluate(*args, **kwargs)
        self.last_evaluation = copy.deepcopy(result)
        return result

    @contextmanager
    def human_checkpoint(self, session_id, action):
        """Arrange only a literal fixture's historical pending-human action."""
        if getattr(self, "_human_checkpoint", None):
            raise ValueError("Nested historical checkpoints are not supported")
        self._human_checkpoint = (session_id, copy.deepcopy(action))
        try:
            yield
        finally:
            self._human_checkpoint = None


class FixtureHistory(PublicHistoryLifecycle):
    def _decision(self, event, action):
        if event["decision"]["resolver_origin"] == "human" and action:
            with self.client.human_checkpoint(self.session_id, action):
                return super()._decision(event, action)
        return super()._decision(event, action)


class ModeFactory(LocalFactory):
    def __init__(self, *args, mode, **kwargs):
        super().__init__(*args, **kwargs)
        self.mode = mode

    def _runtime(self, arm, backend="llm", judge="advisory"):
        if backend != "llm" or judge != "advisory":
            raise ValueError(
                "Matched release requires fixed LLM evaluator and advisory Judge"
            )
        key = (self.mode, backend, judge)
        if key not in self.runtimes:
            self.runtimes[key] = Runtime(
                self.path,
                self.revision,
                self.directory / "-".join(key),
                self.relay,
                backend,
                judge,
                mode=self.mode,
                intention_enabled=True,
                context_mode="source_context" if self.mode == "hybrid_v1" else None,
                managed_proxy=True,
            )
        return self.runtimes[key]

    def open_history(
        self,
        *,
        arm,
        trial,
        backend,
        judge,
        relay,
        initial_assignment,
        policy=None,
        details=None,
        session_id=None,
    ):
        runtime = self._runtime(arm, backend, judge)
        session_id = session_id or secrets.token_hex(16)
        client = ModeClient(
            runtime.url,
            runtime.api_key,
            session_id,
            "release-benchmark",
            request_retries=0,
            timeout_s=1200,
            mode=self.mode,
            evidence_path=self.directory.parent / "sessions.jsonl",
        )
        try:
            client.create_session(
                session_id,
                initial_assignment,
                policy=policy,
                details=details,
            )
            return FixtureHistory(
                client, session_id, SourceHooks(runtime, relay, client)
            )
        except BaseException:
            client.close()
            raise


def completeness(jobs, rows, error, modes=("legacy", "hybrid_v1")):
    expected = {
        (mode, job["lane"], job["case_id"], job["trial"], step)
        for mode in modes
        for job in jobs
        for step in range(len(job["packet"]["steps"]))
    }
    observed, unestablished, duplicates = set(), [], []
    for row in rows:
        key = tuple(
            row[field] for field in ("mode", "lane", "case_id", "trial", "step_index")
        )
        if key in observed:
            duplicates.append(key)
        observed.add(key)
        result = row["result"]
        terminal = result.get("terminal", {})
        if (
            result.get("state") == "UNESTABLISHED"
            or result.get("error")
            or terminal.get("state") not in {"terminal", "human_required"}
            or terminal.get("decision") not in {"approve", "deny", "escalate"}
            or not terminal.get("audit")
        ):
            unestablished.append(key)
    return {
        "planned_rows": len(expected),
        "collected_rows": len(rows),
        "missing": sorted(expected - observed),
        "unexpected": sorted(observed - expected),
        "duplicates": duplicates,
        "unestablished": unestablished,
        "denominator_complete": observed == expected and not duplicates,
        "collection_complete": (
            error is None
            and observed == expected
            and not duplicates
            and not unestablished
        ),
    }


def select_jobs(jobs, selected_cases=None):
    full_count = sum(len(job["packet"]["steps"]) for job in jobs)
    if selected_cases:
        missing = set(selected_cases) - {job["case_id"] for job in jobs}
        if missing:
            raise ValueError(f"Unknown selected cases: {sorted(missing)}")
        jobs = [job for job in jobs if job["case_id"] in selected_cases]
    return jobs, {
        "selected_case_ids": sorted(set(selected_cases or [])),
        "supplemental": bool(selected_cases),
        "full_corpus_rows_per_mode": full_count,
        "planned_rows_per_mode": sum(len(job["packet"]["steps"]) for job in jobs),
    }


def run(args):
    selected_modes = tuple(getattr(args, "modes", ("legacy", "hybrid_v1")))
    if not selected_modes or len(set(selected_modes)) != len(selected_modes):
        raise ValueError("Select distinct benchmark modes")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    manifest = json.loads(Path(args.manifest).read_text())
    manifest = manifest.get("corpus", manifest)
    jobs, selection = select_jobs(
        planned_jobs(manifest), getattr(args, "case_ids", None)
    )
    previous = load_previous_completion(args.previous_completion)["budget"]
    proxy = None
    factories = {}
    rows = []
    error = None
    try:
        proxy = DirectProxy(
            output,
            os.environ["COGNIS_API_KEY"],
            previous,
            base_url=os.environ["COGNIS_BENCHMARK_BASE_URL"],
        )
        for mode in selected_modes:
            factories[mode] = ModeFactory(
                runtime=Path(args.runtime),
                revision=args.revision,
                directory=output / mode,
                relay=proxy,
                proof={},
                mode=mode,
            )
        write_json(
            output / "manifest.json",
            {
                "revision": args.revision,
                "corpus": manifest,
                "modes": list(factories),
                "trials": 3,
                "models": proxy.binding,
                "reasoning": {
                    "evaluator": "low",
                    "intention": "low",
                    "judge": "medium",
                },
                **selection,
                "extended_harness_timeouts_not_production_latency": True,
                "fixture_identity_isolated": True,
                "historical_human_setup": "literal fixture action with request-only escalation floor",
                "previous_completion": args.previous_completion,
            },
        )
        for index, job in enumerate(jobs):
            # Alternate mode order rather than putting one complete run first.
            modes = list(factories)
            if index % 2:
                modes.reverse()
            for mode in modes:
                proxy.context = {k: v for k, v in job.items() if k != "packet"}
                proxy.context.update(mode=mode, backend="llm")
                results = factories[mode].replay(
                    packet=job["packet"],
                    arm="source_context",
                    backend="llm",
                    judge="advisory",
                    trial=job["trial"],
                    relay=proxy,
                    session_id=secrets.token_hex(16),
                )
                for step, result in enumerate(results):
                    row = {**proxy.context, "step_index": step, "result": result}
                    rows.append(row)
                    with (output / "rows.jsonl").open("a") as stream:
                        stream.write(json.dumps(row) + "\n")
                if proxy.budget.stop_reason:
                    raise RuntimeError("Benchmark budget or transport stopped")
        proxy.checked_metadata()
    except BaseException as exc:
        error = type(exc).__name__
        raise
    finally:
        write_json(
            output / "completion.json",
            {
                "error": error,
                **completeness(jobs, rows, error, selected_modes),
                "budget": proxy.budget.snapshot() if proxy else previous,
                "uncollected_are_not_safe_outcomes": True,
            },
        )
        for factory in factories.values():
            factory.close()
        if proxy:
            proxy.close()


def main():
    parser = argparse.ArgumentParser()
    for flag in ("runtime", "revision", "manifest", "previous-completion", "output"):
        parser.add_argument("--" + flag, required=True)
    parser.add_argument(
        "--modes",
        nargs="+",
        choices=("legacy", "hybrid_v1"),
        default=["legacy", "hybrid_v1"],
        help="Select candidate-only replay when retained legacy evidence is sufficient.",
    )
    parser.add_argument(
        "--case-ids",
        nargs="+",
        help="Explicit supplemental cases; never replaces a full-run denominator.",
    )
    run(parser.parse_args())


if __name__ == "__main__":
    main()
