"""Full frozen source/history candidate replay, balanced only, via Cognis.

Reuses public lifecycle and primary replay owners. Never dispatches fixture tools.
Historical decisions are literal isolated fixture events, not real user consent.
"""

import argparse
import copy
import json
import secrets
import time
from collections import Counter
from pathlib import Path

from tools.benchmark.matched import Runtime, digest, harness_sources, write_json
from tools.benchmark.release_proxy import (
    Proxy,
    RecordedClient,
    load_previous_completion,
)
from tools.benchmark.source_context import extraction_check, schedule, verify
from tools.benchmark.source_history_manifest import (
    checkpoint_contract,
    history_prefix,
    load_manifest,
    manifest_sha256,
)
from tools.benchmark.source_history_runner import (
    PublicHistoryLifecycle,
    Unestablished,
    probe_observation,
)
from tools.benchmark.source_local_factory import LocalFactory, LocalHooks
from tools.benchmark.terminal import read_terminal


def planned_jobs(manifest):
    verify(manifest)
    return [
        job
        for job in schedule(manifest)
        if job["arm"] == "source_context"
        and job["backend"] == "jev"
        and job["judge"] == "advisory"
    ]


class SourceClient(RecordedClient):
    last_evaluation = None
    review_timeout_s = 600

    def evaluate(self, *args, **kwargs):
        result = super().evaluate(*args, **kwargs)
        self.last_evaluation = copy.deepcopy(result)
        return result


class SourceHooks(LocalHooks):
    """Observe isolated persisted profiles; single adaptation makes no model call."""

    def capture(self, *, client, session_id):
        from intaris.audit import AuditStore
        from intaris.config import DBConfig
        from intaris.db import Database
        from intaris.intention_profile import adapt
        from intaris.session import SessionStore
        from intaris.task_context import TaskContext

        db = Database(DBConfig(path=str(self.runtime.directory / "intaris.db")))
        try:
            store = SessionStore(db)
            owner = TaskContext(
                llm=None,
                sessions=store,
                audit=AuditStore(db),
                extractor_identity={"generation": "single"},
                generation="single",
            )
            deadline = time.monotonic() + 600
            while True:
                session = store.get(session_id, user_id=client.user_id)
                captured = owner.collect(session)
                _, status = adapt(captured)
                if status != "unavailable" or time.monotonic() >= deadline:
                    break
                time.sleep(0.1)
            artifact = owner.capture(session)
            return {
                "artifact": artifact,
                "profile": captured.get("profile"),
                "generation_status": "ready"
                if status != "unavailable"
                else "UNESTABLISHED",
                "source_digest": artifact["source_digest"],
                "coverage": artifact["coverage"],
                "source_watermark": artifact["watermarks"]["messages"],
                "decision_watermark": artifact["watermarks"]["decisions"],
                "generated_intention": session["intention"],
                "latest_user_text": session.get("latest_user_message"),
                "evidence_level": "persisted_profile_component",
                "component_only": True,
                "cache_state": "not_applicable",
            }
        finally:
            db.close()


class SourceFactory(LocalFactory):
    review_timeout_s = 600

    def _runtime(self, arm, backend="jev", judge="advisory"):
        if (arm, backend, judge) != ("source_context", "jev", "advisory"):
            raise ValueError("Only the frozen current candidate is supported")
        key = (arm, backend, judge)
        if key not in self.runtimes:
            self.runtimes[key] = Runtime(
                self.path,
                self.revision,
                self.directory / "-".join(key),
                self.relay,
                backend,
                judge,
                mode="hybrid_v1",
                intention_enabled=True,
                context_mode=arm,
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
        client = SourceClient(
            runtime.url,
            runtime.api_key,
            "release-benchmark",
            "release-benchmark",
            request_retries=0,
            timeout_s=1200,
            evidence_path=self.directory.parent / "sessions.jsonl",
        )
        try:
            session_id = session_id or secrets.token_hex(16)
            client.create_session(
                session_id,
                initial_assignment,
                policy=policy,
                details=details,
            )
            return PublicHistoryLifecycle(
                client,
                session_id,
                SourceHooks(runtime, relay, client),
            )
        except BaseException:
            client.close()
            raise


def run_history_story(factory, proxy, data, story, trial, output):
    lifecycle = factory.open_history(
        arm="source_context",
        backend="jev",
        judge="advisory",
        trial=trial,
        relay=proxy,
        initial_assignment="Await recorded user instructions.",
    )
    seen = set()
    try:
        for checkpoint in story["checkpoints"]:
            proxy.context = {
                "lane": "history",
                "trial": trial,
                "story": story["id"],
                "checkpoint": checkpoint["id"],
            }
            row = dict(proxy.context)
            row["probe_expected"] = "probe" in checkpoint
            prefix = history_prefix(data, story["id"], checkpoint["id"])
            try:
                for event in prefix["events"]:
                    if event["id"] in seen:
                        continue
                    action = prefix["action_bindings"].get(
                        event.get("decision", {}).get("action_binding"),
                    )
                    try:
                        lifecycle.record(
                            event,
                            origin=prefix["actors"][event["actor"]],
                            action=action,
                        )
                    except Unestablished as exc:
                        lifecycle.omissions.append(
                            {"event": event["id"], "kind": str(exc)}
                        )
                    seen.add(event["id"])
                row["snapshot"] = lifecycle.hooks.capture(
                    client=lifecycle.client,
                    session_id=lifecycle.session_id,
                )
                row["recorded_reference_map"] = copy.deepcopy(lifecycle.records)
                row["recording_omissions"] = copy.deepcopy(lifecycle.omissions)
                # Expectations enter reporting only after runtime observation.
                oracle = checkpoint_contract(story, checkpoint["id"])
                row["oracle"] = oracle
                row["declared_coverage"] = oracle["coverage"]
                row["observed_coverage"] = row["snapshot"]["coverage"]
                row["coverage_verdict"] = "pending_independent_adjudication"
                if "probe" in checkpoint:
                    probe = checkpoint["probe"]
                    response = lifecycle.client.evaluate(
                        lifecycle.session_id,
                        probe["tool"],
                        copy.deepcopy(probe["args"]),
                    )
                    terminal = read_terminal(
                        lifecycle.client,
                        response,
                        judge_enabled=True,
                        timeout_s=600,
                    )
                    row.update(probe=response, probe_terminal=terminal)
                    row["probe_observation"] = probe_observation(
                        response,
                        terminal,
                        probe["authorization"],
                    )
                row["semantic_verdict"] = "pending_independent_adjudication"
            except Exception as exc:
                row["error"] = type(exc).__name__
            append(output, row)
            yield row
            if proxy.stopped:
                raise RuntimeError("Cumulative provider budget stopped")
    finally:
        lifecycle.hooks.close()


def append(path, row):
    with path.open("a") as stream:
        stream.write(json.dumps(row) + "\n")


def qualification(primary_rows, history_rows, plan, error):
    denominator_complete = (
        len(primary_rows) == plan["primary_observations"]
        and len(history_rows) == plan["history_checkpoints"]
    )
    primary_errors = sum(
        bool(r["result"].get("error"))
        or r["result"].get("terminal", {}).get("state")
        not in {"terminal", "human_required"}
        for r in primary_rows
    )
    history_errors = sum(
        bool(r.get("error"))
        or bool(r.get("recording_omissions"))
        or r.get("snapshot", {}).get("generation_status") != "ready"
        or (
            r.get("probe_expected")
            and not r.get("probe_observation", {}).get("terminal_verified")
        )
        for r in history_rows
    )
    return {
        "denominator_complete": denominator_complete,
        "primary_unestablished": primary_errors,
        "history_unestablished": history_errors,
        "collection_complete": denominator_complete
        and error is None
        and primary_errors == 0
        and history_errors == 0,
        "semantic_adjudication_complete": False,
    }


def run(args):
    output = Path(args.output).resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    manifest = json.loads(Path(args.manifest).read_text())
    jobs = planned_jobs(manifest)
    history = load_manifest()
    plan = {
        "runtime": args.revision,
        "profile": "balanced",
        "candidate": {
            "backend": "jev",
            "judge": "advisory",
            "context": "source_context",
        },
        "source_manifest_hash": manifest["sha256"],
        "history_hash": manifest_sha256(),
        "harness_sources": harness_sources(),
        "primary_observations": sum(j["expected_calls"] for j in jobs),
        "history_checkpoints": 3
        * sum(len(s["checkpoints"]) for s in history["stories"]),
        "historical_legacy_not_rerun": True,
        "jobs": jobs,
    }
    plan["sha256"] = digest(plan)
    write_json(output / "plan.json", plan)
    previous = load_previous_completion(
        args.previous_completion,
        reviewed_transport_restart=args.reviewed_transport_restart,
    )["budget"]
    binding = json.loads(Path(args.binding).read_text())
    proxy = factory = None
    primary_rows, history_rows = [], []
    error = None
    try:
        proxy = Proxy(
            output,
            binding,
            previous,
            reviewed_transport_restart=args.reviewed_transport_restart,
        )
        proxy.validation_errors = []
        factory = SourceFactory(
            runtime=Path(args.runtime),
            revision=args.revision,
            directory=output / "runtime",
            relay=proxy,
            proof={},
        )
        # First source-bearing cases are a coverage gate, not another paid corpus.
        jobs.sort(
            key=lambda j: (
                j["case_id"] != "email-requested",
                not bool(j["packet"].get("events")),
            )
        )
        for job in jobs:
            proxy.context = {
                k: v for k, v in job.items() if k not in {"packet", "expected_calls"}
            }
            before = len(proxy.records)
            batch = factory.replay(
                packet=copy.deepcopy(job["packet"]),
                arm=job["arm"],
                backend=job["backend"],
                judge=job["judge"],
                trial=job["trial"],
                relay=proxy,
                session_id=secrets.token_hex(16),
            )
            if len(batch) != job["expected_calls"]:
                raise RuntimeError("Primary replay changed planned denominator")
            for index, result in enumerate(batch):
                row = {**proxy.context, "step_index": index, "result": result}
                row["extraction_validation"] = extraction_check(
                    job["packet"],
                    result.get("task_context"),
                )
                primary_rows.append(row)
                append(output / "primary.jsonl", row)
            if len(primary_rows) == 1:
                if not any(r["stage"] == "intention" for r in proxy.records[before:]):
                    raise RuntimeError(
                        "Source preflight produced no intention inference"
                    )
                if not batch[0].get("task_context"):
                    raise RuntimeError("Source preflight captured no task context")
                artifact = batch[0]["task_context"]
                runtime = factory._runtime("source_context")
                with SourceClient(
                    runtime.url,
                    runtime.api_key,
                    "release-benchmark",
                    "release-benchmark",
                    request_retries=0,
                    evidence_path=output / "sessions.jsonl",
                ) as observer:
                    observed = observer.get_session(artifact["session"])
                if not observed.get("intention_profile"):
                    raise RuntimeError(
                        "Source preflight persisted no intention profile"
                    )
                write_json(
                    output / "source-preflight.json",
                    {
                        "case_id": job["case_id"],
                        "session": observed,
                        "provider_attempts": proxy.records[before:],
                        "task_context": artifact,
                    },
                )
            proxy.drain()
            if proxy.stopped:
                raise RuntimeError("Cumulative provider budget stopped")
        for trial in range(1, 4):
            for story in history["stories"]:
                history_rows.extend(
                    run_history_story(
                        factory,
                        proxy,
                        history,
                        story,
                        trial,
                        output / "history.jsonl",
                    )
                )
        with proxy.lock:
            post = proxy.checked_metadata()
        proxy.verify_binding(post, proxy.binding)
        write_json(output / "post-run-binding.json", post)
        write_json(output / "final-transport-binding.json", proxy.binding)
    except BaseException as exc:
        error = type(exc).__name__ + ": " + str(exc)
        raise
    finally:
        if factory:
            factory.close()
        if proxy:
            proxy.close()
        write_json(
            output / "completion.json",
            {
                "plan_hash": plan["sha256"],
                "error": error,
                "primary_collected": len(primary_rows),
                "primary_planned": plan["primary_observations"],
                "history_collected": len(history_rows),
                "history_planned": plan["history_checkpoints"],
                "budget": proxy.budget.snapshot() if proxy else previous,
                "prior_run_accounting": previous,
                "reviewed_transport_restart": args.reviewed_transport_restart,
                "stage_attempts": dict(Counter(r["stage"] for r in proxy.records))
                if proxy
                else {},
                **qualification(primary_rows, history_rows, plan, error),
            },
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reviewed-transport-restart")
    for name in (
        "runtime",
        "revision",
        "manifest",
        "binding",
        "previous-completion",
        "output",
    ):
        parser.add_argument("--" + name, required=True)
    run(parser.parse_args())
