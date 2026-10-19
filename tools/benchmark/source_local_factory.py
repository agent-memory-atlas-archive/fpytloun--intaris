"""Executable loopback runtime factory; artifacts come from actual API/audit.

Core imports for labelled component generation happen in a fresh subprocess
with the approved source tree first on sys.path. No cache observer is used.
"""

from __future__ import annotations

import argparse
import copy
import json
import secrets
import subprocess
import sys
import time
from pathlib import Path

from tools.benchmark.client import IntarisClient, IntarisError
from tools.benchmark.matched import Runtime, digest, runtime_revision
from tools.benchmark.source_history_runner import Unestablished
from tools.benchmark.terminal import read_terminal


def api_artifact(response: dict, audit: dict, *, session_id: str, user_id: str) -> dict:
    call_id = response.get("call_id")
    if (
        not call_id
        or audit.get("call_id") != call_id
        or audit.get("session_id") != session_id
    ):
        raise ValueError("Artifact must bind to actual call/session")
    artifact = (response.get("evaluation_metadata") or {}).get("task_context")
    stored = (audit.get("evaluation_metadata") or {}).get("task_context")
    if artifact is None:
        artifact = stored
    elif stored is not None and stored != artifact:
        raise ValueError("Response and audit captured artifacts differ")
    if artifact is None:
        return {"state": "MISSING", "evidence_level": "public_action_api"}
    if artifact.get("tenant") != user_id or artifact.get("session") != session_id:
        raise ValueError("Captured artifact tenant/session mismatch")
    if not artifact.get("artifact_digest") or not artifact.get("source_digest"):
        raise ValueError("Captured artifact identity missing")
    return {
        "state": "CAPTURED",
        "artifact": copy.deepcopy(artifact),
        "call_id": call_id,
        "evidence_level": "public_action_api",
    }


class LocalClient(IntarisClient):
    def __init__(self, runtime, user_id="benchmark"):
        super().__init__(
            runtime.url,
            runtime.api_key,
            user_id=user_id,
            agent_id="source-context-benchmark",
        )
        self.runtime, self.last_evaluation = runtime, None

    def evaluate(self, session_id, tool, args, **kwargs):
        result = super().evaluate(session_id, tool, args, **kwargs)
        self.last_evaluation = copy.deepcopy(result)
        return result


class LocalHooks:
    def __init__(self, runtime, relay, client):
        self.runtime, self.relay, self.client = runtime, relay, client

    def capture(self, *, client, session_id):
        # Context-only checkpoints deliberately generate from actual DB state.
        # Never label this as observation of an action-admission artifact.
        output = self.runtime.directory / (
            "component-" + digest(session_id)[:16] + ".json"
        )
        config = {
            "runtime": str(self.runtime.path),
            "db": str(self.runtime.directory / "intaris.db"),
            "session_id": session_id,
            "user_id": client.user_id,
            "relay_url": self.relay.url,
            "relay_token": self.relay.token,
        }
        run = subprocess.run(
            [
                sys.executable,
                "-m",
                "tools.benchmark.source_local_factory",
                "component",
                "--output",
                str(output),
            ],
            input=json.dumps(config),
            text=True,
            capture_output=True,
            timeout=30,
        )
        if run.returncode:
            (self.runtime.directory / "component-error.log").write_text(run.stderr)
            raise Unestablished(
                "Component generation unavailable; subprocess diagnostics private"
            )
        data = json.loads(output.read_text())
        data["evidence_level"] = "component_generation"
        data["component_only"] = True
        return data

    def observe_action(self, session_id):
        response = self.client.last_evaluation
        if not response:
            return {"state": "MISSING", "evidence_level": "public_action_api"}
        audit = self.client.get_audit_record(response["call_id"])
        return api_artifact(
            response, audit, session_id=session_id, user_id=self.client.user_id
        )

    def observe_judge_resolution(self, *, client, session_id, action):
        response = client.evaluate(
            session_id, action["tool"], copy.deepcopy(action["args"])
        )
        terminal = read_terminal(client, response, judge_enabled=True)
        audit = terminal.get("audit") or {}
        if audit.get("resolved_by") != "judge":
            raise Unestablished("Actual server did not produce Judge-origin resolution")
        return audit

    def close(self):
        self.client.close()


class LocalFactory:
    def __init__(
        self, *, runtime: Path, revision: str, directory: Path, relay, proof: dict
    ):
        self.path, self.revision, self.directory = (
            runtime.resolve(),
            revision,
            directory,
        )
        self.relay, self.proof, self.runtimes = relay, copy.deepcopy(proof), {}
        if runtime_revision(self.path) != revision:
            raise ValueError("Approved runtime revision mismatch")
        if subprocess.check_output(
            ["git", "-C", str(self.path), "status", "--porcelain"], text=True
        ):
            raise ValueError("Approved runtime must be clean")

    def validate(self, config):
        if self.proof.get("approved_revision") != self.revision:
            raise ValueError("Runtime evidence revision differs from actual factory")
        required = ("clean_runtime", "role_recording_verified")
        if any(
            not isinstance(self.proof.get(key), dict)
            or self.proof[key].get("passed") is not True
            for key in required
        ):
            raise ValueError(
                "Canonical proof lacks established runtime/lifecycle evidence"
            )
        return {
            "approved_revision": self.revision,
            "clean_runtime": True,
            "public_lifecycle_verified": True,
            "evidence_reference": self.proof["role_recording_verified"]["reference"],
            "canonical_proof": copy.deepcopy(self.proof),
        }

    def preflight(self, config):
        self.validate(config)
        return copy.deepcopy(self.proof)

    def _runtime(self, arm, backend="llm", judge="advisory"):
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
                context_mode={
                    "baseline": "mixed",
                    "mixed": "mixed",
                    "source_context": "source_context",
                }[arm],
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
        client = LocalClient(runtime)
        try:
            from tools.benchmark.source_history_runner import PublicHistoryLifecycle

            session_id = session_id or secrets.token_hex(16)
            client.create_session(
                session_id,
                initial_assignment,
                policy=copy.deepcopy(policy),
                details=copy.deepcopy(details),
            )
            return PublicHistoryLifecycle(
                client, session_id, LocalHooks(runtime, relay, client)
            )
        except BaseException:
            client.close()
            raise

    def open(self, *, arm, backend, judge, session_id):
        lifecycle = self.open_history(
            arm=arm,
            trial=None,
            backend=backend,
            judge=judge,
            relay=self.relay,
            initial_assignment="Await recorded user instructions.",
            session_id=session_id,
        )
        return lifecycle.client, lifecycle.hooks

    def replay(self, *, packet, arm, backend, judge, trial, relay, session_id):
        lifecycle = self.open_history(
            arm=arm,
            trial=trial,
            backend=backend,
            judge=judge,
            relay=relay,
            initial_assignment=packet["intention"],
            policy=packet.get("policy"),
            details=packet.get("details"),
            session_id=session_id,
        )
        rows = []
        setup_records = []
        try:
            # Preserve original policy/clues exactly; if activation is rejected
            # this is a real unsupported row, never candidate-only injection.
            for index, event in enumerate(packet.get("events", [])):
                if event.get("mechanism"):
                    if (
                        event["mechanism"] != "public_human_resolution"
                        or event.get("role") != "decision"
                        or event.get("decision") not in {"approve", "deny"}
                        or not isinstance(event.get("action"), dict)
                        or not isinstance(event["action"].get("tool"), str)
                        or not isinstance(event["action"].get("args"), dict)
                    ):
                        return [
                            {
                                "state": "UNESTABLISHED",
                                "error": "Unsupported focused historical decision binding",
                                "historical_setup": copy.deepcopy(setup_records),
                                "terminal": {"state": "unverified", "decision": None},
                            }
                            for _ in packet["steps"]
                        ]
                    previous_context = copy.deepcopy(relay.context)
                    before = len(relay.records)
                    before_validation = len(relay.validation_errors)
                    relay.context = {
                        **previous_context,
                        "quality_role": "historical_setup_control",
                        "setup_event_index": index,
                    }
                    setup = {
                        "event_index": index,
                        "quality_role": "historical_setup_control",
                        "action": copy.deepcopy(event["action"]),
                        "requested_resolution": event["decision"],
                        "state": "pending",
                        "counted_in_shared_budget": True,
                        "primary_quality_row": False,
                    }
                    setup_records.append(setup)
                    previous_evaluation = copy.deepcopy(
                        lifecycle.client.last_evaluation
                    )
                    failure = None
                    try:
                        # Serialize only the literal decision and historical
                        # action, not same/different-action scorer labels.
                        record = lifecycle.record(
                            {
                                "id": str(index),
                                "kind": "decision",
                                "text": json.dumps(
                                    {
                                        "action": event["action"],
                                        "decision": event["decision"],
                                    },
                                    sort_keys=True,
                                ),
                                "decision": {
                                    "resolver_origin": "human",
                                    "resolution": event["decision"],
                                },
                            },
                            origin="human",
                            action=copy.deepcopy(event["action"]),
                        )
                        setup["record"] = copy.deepcopy(record)
                        setup["state"] = "recorded"
                    except (Unestablished, IntarisError) as exc:
                        failure = type(exc).__name__
                        setup["state"] = "UNESTABLISHED"
                        setup["error_type"] = failure
                        if isinstance(exc, IntarisError):
                            setup["http_status"] = exc.status_code
                    finally:
                        try:
                            relay.drain()
                        except RuntimeError as exc:
                            failure = type(exc).__name__
                            setup["state"] = "UNESTABLISHED"
                            setup["drain_error_type"] = failure
                        setup["provider_attempt_indices"] = list(
                            range(before, len(relay.records))
                        )
                        setup["provider_validation_errors"] = copy.deepcopy(
                            relay.validation_errors[before_validation:]
                        )
                        setup["cumulative_budget"] = (
                            relay.budget.snapshot()
                            if relay.budget is not None
                            else None
                        )
                        if relay.stopped or (
                            relay.budget is not None
                            and relay.budget.snapshot().get("stop_reason")
                        ):
                            failure = failure or "ProviderBoundaryStopped"
                            setup["state"] = "UNESTABLISHED"
                            setup["provider_stop"] = True
                        latest = lifecycle.client.last_evaluation
                        if latest and latest != previous_evaluation:
                            setup["initial_evaluation"] = copy.deepcopy(latest)
                            if latest.get("call_id"):
                                setup["call_id"] = latest["call_id"]
                                if "record" not in setup:
                                    try:
                                        setup["available_audit"] = copy.deepcopy(
                                            lifecycle.client.get_audit_record(
                                                latest["call_id"]
                                            )
                                        )
                                    except IntarisError as exc:
                                        setup["audit_readback_http_status"] = (
                                            exc.status_code
                                        )
                        relay.context = previous_context
                    if failure:
                        return [
                            {
                                "state": "UNESTABLISHED",
                                "error": failure,
                                "historical_setup": copy.deepcopy(setup_records),
                                "terminal": {"state": "unverified", "decision": None},
                            }
                            for _ in packet["steps"]
                        ]
                    continue
                lifecycle.record(
                    {"id": str(index), "kind": "message", "text": event["text"]},
                    origin={
                        "user": "client_attributed_user",
                        "assistant": "agent",
                        "agent": "agent",
                        "tool": "tool",
                    }[event["role"]],
                )
            for step in packet["steps"]:
                if step.get("latest_user_message"):
                    lifecycle.client.submit_reasoning(
                        lifecycle.session_id,
                        "User message: " + step["latest_user_message"],
                    )
                start = time.monotonic()
                result = lifecycle.client.evaluate(
                    lifecycle.session_id,
                    step["tool"],
                    copy.deepcopy(step["args"]),
                    context=step.get("context"),
                )
                terminal = read_terminal(
                    lifecycle.client, result, judge_enabled=judge != "disabled"
                )
                captured = api_artifact(
                    result,
                    lifecycle.client.get_audit_record(result["call_id"]),
                    session_id=lifecycle.session_id,
                    user_id=lifecycle.client.user_id,
                )
                rows.append(
                    {
                        "intaris": result,
                        "terminal": terminal,
                        "task_context": captured.get("artifact"),
                        "captured_artifact": captured,
                        "historical_setup": copy.deepcopy(setup_records),
                        "end_to_end_ms": (time.monotonic() - start) * 1000,
                    }
                )
            return rows
        finally:
            lifecycle.hooks.close()

    def close(self):
        for runtime in self.runtimes.values():
            runtime.close()
        self.runtimes.clear()


def component(config):
    sys.path.insert(0, config["runtime"])
    from intaris.audit import AuditStore
    from intaris.config import DBConfig, LLMConfig
    from intaris.db import Database
    from intaris.llm import LLMClient
    from intaris.session import SessionStore
    from intaris.task_context import TaskContext

    db = Database(DBConfig(path=config["db"]))
    sessions = SessionStore(db)
    session = sessions.get(config["session_id"], user_id=config["user_id"])
    owner = TaskContext(
        llm=LLMClient(
            LLMConfig(
                model="openai/gpt-oss-120b",
                base_url=config["relay_url"] + "/intention",
                api_key=config["relay_token"],
                reasoning_effort="low",
            )
        ),
        sessions=sessions,
        audit=AuditStore(db),
        extractor_identity={
            "model": "openai/gpt-oss-120b",
            "reasoning_effort": "low",
            "endpoint_digest": digest(config["relay_url"] + "/intention"),
        },
    )
    artifact = owner.capture(session)
    return {
        "artifact": artifact,
        "source_digest": artifact["source_digest"],
        "coverage": artifact["coverage"],
        "cache_state": "cold",
        "source_watermark": artifact["watermarks"]["messages"],
        "decision_watermark": artifact["watermarks"]["decisions"],
        "latest_user_text": session.get("latest_user_message"),
        "generated_intention": session.get("intention"),
        "generation_status": "UNESTABLISHED",
        "semantic_projection_origin": None,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["component"])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    from tools.benchmark.matched import write_json

    write_json(args.output, component(json.loads(sys.stdin.read())))


if __name__ == "__main__":
    main()
