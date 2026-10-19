"""Prepare 300 private write samples; running paid inference requires explicit opt-in.

Preparation only reads PostgreSQL and never constructs an inference client.
Execution uses a temporary local DB and never dispatches tools or writes production.
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.rc3_replay import private_write
from tools.static_comparison import load_rows, query


def stratum(row):
    if (
        row["decision"] != "approve"
        or row.get("hybrid")
        or row.get("evaluation_path") != "llm"
    ):
        return None
    tool = row["tool"]
    if tool == "bash":
        return "bash"
    if tool in {"apply_patch", "edit", "multiedit", "write"}:
        return "file_writes"
    if tool.startswith("mcp_") and re.search(
        r"__(?:add|create|update|delete|remove|send|post|publish|upload|complete|move|modify|set)[_-]",
        tool,
    ):
        return "mcp_writes"
    return None


def sample(rows):
    groups = {name: [] for name in ("bash", "file_writes", "mcp_writes")}
    for row in rows:
        group = stratum(row)
        if group:
            groups[group].append(row)
    for group in groups.values():
        group.sort(key=lambda row: hashlib.sha256(row["call_id"].encode()).hexdigest())
    selected = []
    while len(selected) < 300 and any(groups.values()):
        for name in groups:
            if groups[name] and len(selected) < 300:
                selected.append({**groups[name].pop(0), "stratum": name})
    if len(selected) != 300:
        raise ValueError("Fewer than 300 qualifying writes; no paid sample produced")
    return selected


def prepare(options):
    selected = sample(load_rows(options.export))
    ids = ",".join("'" + row["call_id"] + "'" for row in selected)
    # IDs were validated by load_rows. Content is written to 0600 files only.
    sql = f"""
    BEGIN READ ONLY;
    SET LOCAL statement_timeout='120s';
    SELECT json_build_object(
      'call_id',a.call_id,'intention',COALESCE(a.intention,s.intention),'timestamp',a.timestamp,
      'intention_source',CASE WHEN a.intention IS NULL THEN 'current_session' ELSE 'audit_intention' END,
      'user_messages',COALESCE((
        SELECT json_agg(json_build_object('content',r.content,'timestamp',r.timestamp)
                        ORDER BY r.timestamp)
        FROM audit_log r WHERE r.user_id=a.user_id AND r.session_id=a.session_id
        AND r.record_type='reasoning'
        AND r.timestamp::timestamptz <= a.timestamp::timestamptz
        AND (r.content LIKE 'User message:%' OR r.args_redacted::jsonb->'context'->>'role'='user'
             OR r.args_redacted::jsonb->>'role'='user')
      ),'[]'::json)
    ) FROM audit_log a JOIN sessions s ON s.user_id=a.user_id AND s.session_id=a.session_id
    WHERE a.call_id IN ({ids});
    COMMIT;
    """
    evidence = {row["call_id"]: row for row in query(sql)}
    dataset = []
    for row in selected:
        captured = row["metadata"].get("task_context") or {}
        originals = captured.get("originals") or []
        historical = next(
            (r["content"] for r in originals if r.get("ref") == "session_intention"),
            None,
        )
        dataset.append(
            {
                "call_id": row["call_id"],
                "session_id": row["session_id"],
                "tool": row["tool"],
                "args": row["args"],
                "policy": row["policy"],
                "details": row["details"],
                "stratum": row["stratum"],
                "intention": historical or evidence[row["call_id"]]["intention"],
                "intention_source": "audit_snapshot"
                if historical
                else evidence[row["call_id"]]["intention_source"],
                "user_messages": evidence[row["call_id"]]["user_messages"],
                "legacy_decision": "approve",
            }
        )
    private_write(options.output / "paid-input.json", dataset)
    estimate = estimate_cost(dataset, options.input_rate, options.output_rate)
    private_write(options.output / "paid-estimate.json", estimate)
    print(json.dumps(estimate))


def estimate_cost(dataset, input_rate=0.15, output_rate=0.60):
    """Use measured action cost, with separately priced profile generation."""
    import math
    import statistics

    def tokens(value):
        # Actual sample text, not four copies of its UTF-8 byte count.
        # GPT-OSS tokenizer is not bundled; label this approximation explicitly.
        return math.ceil(len(json.dumps(value, ensure_ascii=False)) / 4)

    action = [
        tokens({k: row[k] for k in ("tool", "args", "intention", "user_messages")})
        for row in dataset
    ]
    profile = [
        tokens({k: row[k] for k in ("intention", "user_messages")}) + 1500
        for row in dataset
    ]
    sessions = {}
    for row, count in zip(dataset, profile):
        sessions.setdefault(row.get("session_id", row["call_id"]), count)

    def summary(values):
        ordered = sorted(values)
        return {
            "mean": round(statistics.mean(values)),
            "p50": round(statistics.median(values)),
            "p90": ordered[math.ceil(len(ordered) * 0.9) - 1],
            "max": max(values),
        }

    measured = len(dataset) * 0.000412

    def profile_price(values, output):
        return (sum(values) * input_rate + len(values) * output * output_rate) / 1e6

    # Existing isolated runner generates one profile for each sampled action,
    # not one per original session. Report both; do not underprice the command.
    replay_estimate = measured + profile_price(profile, 1024)
    session_estimate = measured + profile_price(list(sessions.values()), 1024)
    # Four stages, twice estimated text size + 4000 schema tokens per stage,
    # full output caps, and a 3-attempt allowance. Conditional planning upper
    # bound, not a hard billing cap for dynamic managed routes.
    upper_input = sum(4 * (2 * count + 4000) for count in action)
    upper_output = len(dataset) * (8192 + 4096 * 3)
    return {
        "samples": len(dataset),
        "original_sessions": len(sessions),
        "strata": dict(Counter(row.get("stratum", "unspecified") for row in dataset)),
        "token_method": "Actual JSON characters / 4; estimate, not GPT-OSS tokenizer measurement",
        "action_context_tokens": summary(action),
        "profile_prompt_tokens": summary(profile),
        "measured_action_usd": 0.000412,
        "actions_including_judge_usd": round(measured, 6),
        "profile_input_rate_per_million": input_rate,
        "profile_output_rate_per_million": output_rate,
        "profile_assumed_output_tokens": 1024,
        "session_amortized_estimate_usd": round(session_estimate, 4),
        "isolated_runner_profile_calls": len(dataset),
        "estimated_usd": round(replay_estimate, 4),
        "upper_bound_usd": round(
            3 * (upper_input * input_rate + upper_output * output_rate) / 1e6, 2
        ),
        "paid_calls_run": 0,
        "limitations": [
            "Measured action rate is the coordinator's L20 GPT-OSS benchmark, including Judge.",
            "Profile token rates are assumed GPT-OSS-120B rates, not a guaranteed dynamic-route tariff.",
            "One 120B profile per original session is shown separately; isolated replay creates one per sample.",
            "Upper envelope assumes unchanged rates, at most three attempts per four stages and stated token reserves.",
            "Redacted arguments and historical user messages remain local; no paid calls made.",
        ],
    }


def enrich_history(source, output):
    """Same cohort, strictly prior same-tenant/session actions, no tool outputs."""
    dataset = json.loads(source.read_text())
    ids = [row["call_id"] for row in dataset]
    if len(ids) != 300 or len(set(ids)) != 300:
        raise ValueError("Expected exactly 300 unique original calls")
    if not all(re.fullmatch(r"[A-Za-z0-9_-]+", value) for value in ids):
        raise ValueError("Invalid audit identifier")
    if (output / "paid-input.json").exists():
        raise ValueError("Refusing to replace an existing cohort")
    literals = ",".join("'" + value + "'" for value in ids)
    sql = f"""
    BEGIN READ ONLY;
    SET LOCAL statement_timeout='120s';
    SELECT json_build_object(
      'call_id',a.call_id, 'original_assignment',s.original_assignment,
      'recent_actions', COALESCE((
        SELECT json_agg(json_build_object('tool',r.tool,'args_redacted',
          json_build_object('path',r.target)) ORDER BY r.timestamp DESC,r.id DESC)
        FROM (
          SELECT h.tool,h.timestamp,h.id,
            COALESCE(h.args_redacted::jsonb->>'path',
                     h.args_redacted::jsonb->>'file_path',
                     h.args_redacted::jsonb->>'filePath',
                     h.args_redacted::jsonb->>'command',
                     h.args_redacted::jsonb->>'cmd',
                     substring(h.args_redacted::jsonb->>'patchText'
                       from '\\*\\*\\* (?:Update|Add|Delete) File: ([^\\n]+)'), '')
              AS target
          FROM audit_log h
          WHERE h.user_id=a.user_id AND h.session_id=a.session_id
            AND h.record_type='tool_call'
            AND h.timestamp::timestamptz < a.timestamp::timestamptz
          ORDER BY h.timestamp::timestamptz DESC,h.id DESC LIMIT 15
        ) r
      ),'[]'::json)
    )
    FROM audit_log a JOIN sessions s ON s.user_id=a.user_id AND s.session_id=a.session_id
    WHERE a.call_id IN ({literals});
    COMMIT;
    """
    evidence = {row["call_id"]: row for row in query(sql)}
    if set(evidence) != set(ids):
        raise ValueError("Could not reconstruct the exact cohort")
    from intaris.recent_actions import summarize
    from intaris.redactor import redact

    for row in dataset:
        captured = evidence[row["call_id"]]
        row["recent_actions"] = summarize(captured["recent_actions"])
        row["original_assignment"] = redact(captured["original_assignment"])
    private_write(output / "paid-input.json", dataset)
    print(
        json.dumps(
            {
                "calls": len(dataset),
                "with_history": sum(bool(r["recent_actions"]) for r in dataset),
            }
        )
    )


class ReplayStop(BaseException):
    """Never swallowed by product retry/fallback handlers."""


def response_usage(body):
    """OpenAI usage first, then a Cognis response envelope; never infer usage."""
    if not isinstance(body, dict):
        return None
    usage = body.get("usage")
    if isinstance(usage, dict):
        prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
        completion = usage.get("completion_tokens", usage.get("output_tokens"))
        if isinstance(prompt, int) and isinstance(completion, int):
            return prompt, completion
    for key in ("response", "cognis", "result", "data", "metadata"):
        found = response_usage(body.get(key))
        if found is not None:
            return found
    return None


class Meter:
    def __init__(self, input_rate=0.15, output_rate=0.60, max_cost=5.0):
        if min(input_rate, output_rate, max_cost) <= 0:
            raise ValueError("Rates and budget must be positive")
        self.input_rate, self.output_rate = input_rate, output_rate
        self.max_cost = min(max_cost, 5.0)
        self.prompt = self.completion = self.requests = 0
        self.finished = self.transport_calls = 0
        # Non-chat providers (Jev decisions) report their own priced usage.
        self.extra_cost = 0.0
        self.transport_error = False
        self.stop_reason = None

    @property
    def cost(self):
        return (
            self.prompt * self.input_rate + self.completion * self.output_rate
        ) / 1e6 + self.extra_cost

    def stop(self, reason):
        self.stop_reason = reason
        raise ReplayStop(reason)

    def before(self):
        if (
            self.stop_reason
            or self.cost + getattr(self, "prior_cost", 0) >= self.max_cost
        ):
            self.stop(self.stop_reason or "cost_limit")
        self.requests += 1

    def finish(self):
        self.finished += 1
        self.transport_calls += int(self.transport_error)
        if self.finished >= 20 and self.transport_calls / self.finished > 0.10:
            self.stop_reason = self.stop_reason or "transport_error_limit"
        self.transport_error = False


def refresh_token():
    """Copy directly between pod and private file; never expose token to logs."""
    pod = os.environ.get("REPLAY_TOKEN_POD")
    remote = os.environ.get("REPLAY_TOKEN_PATH")
    if not pod or not remote:
        return False
    namespace = os.environ["REPLAY_TOKEN_NAMESPACE"]
    target = Path(os.environ["COGNIS_INFERENCE_TOKEN_FILE"])
    temporary = target.with_suffix(".refresh")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            subprocess.run(
                ["kubectl", "-n", namespace, "exec", pod, "--", "cat", remote],
                stdout=stream,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=30,
            )
        if temporary.stat().st_size == 0:
            return False
        os.replace(temporary, target)
        return True
    finally:
        temporary.unlink(missing_ok=True)


class MeteredTransport(httpx.BaseTransport):
    def __init__(self, meter, transport=None, refresh=refresh_token):
        self.meter = meter
        self.transport = transport or httpx.HTTPTransport(retries=0)
        self.refresh = refresh

    def handle_request(self, request):
        meter = self.meter
        if not hasattr(meter, "events"):
            meter.events = []
        payload = json.loads(request.content) if request.content else {}
        role = str(payload.get("model", "")).split("/")[-1]
        for attempt in range(2):
            meter.before()
            started = time.monotonic()
            event = {
                "role": role,
                "response_mode": payload.get("response_format", {}).get("type"),
            }
            try:
                response = self.transport.handle_request(request)
                response.read()
            except httpx.TransportError as error:
                meter.events.append({**event, "error_class": type(error).__name__})
                meter.transport_error = True
                raise
            try:
                body = response.json()
                usage = response_usage(body)
            except ValueError:
                body = {}
                usage = None
            error = body.get("error", {}) if isinstance(body, dict) else {}
            code = error.get("code") if isinstance(error, dict) else None
            meter.events.append(
                {
                    **event,
                    "status": response.status_code,
                    "code": code if isinstance(code, str) and len(code) < 100 else None,
                    "request_id": response.headers.get("x-request-id"),
                    "latency_ms": round((time.monotonic() - started) * 1000),
                }
            )
            if usage is not None:
                meter.prompt += usage[0]
                meter.completion += usage[1]
                if meter.cost + getattr(meter, "prior_cost", 0) >= meter.max_cost:
                    response.close()
                    meter.stop("cost_limit")
            if response.status_code == 401 and attempt == 0 and self.refresh():
                response.close()
                request.headers["Authorization"] = (
                    "Bearer "
                    + Path(os.environ["COGNIS_INFERENCE_TOKEN_FILE"])
                    .read_text()
                    .strip()
                )
                continue
            if response.status_code >= 500 or response.status_code in {
                401,
                403,
                408,
                429,
            }:
                meter.transport_error = True
            if response.is_success and usage is None:
                response.close()
                meter.stop("usage_unavailable")
            return response
        raise AssertionError("Unreachable refresh loop")

    def close(self):
        self.transport.close()


# Standalone Jev through OpenRouter's decisions API (pinned dated model).
JEV_OPENROUTER_BASE_URL = "https://openrouter.ai/api"
JEV_OPENROUTER_MODEL = "typesafe/jev-1.13-20260917"
# Public OpenRouter pricing (tools/benchmark/fixtures/source-context-public-pricing-v1.json).
JEV_INPUT_RATE = 0.042


class JevMeteredTransport(httpx.BaseTransport):
    """Meter Jev decisions calls into the shared budget and event log."""

    def __init__(self, meter, transport=None):
        self.meter = meter
        self.transport = transport or httpx.HTTPTransport(retries=0)

    def handle_request(self, request):
        meter = self.meter
        if not hasattr(meter, "events"):
            meter.events = []
        meter.before()
        started = time.monotonic()
        event = {"role": "jev", "response_mode": "decisions"}
        try:
            response = self.transport.handle_request(request)
            response.read()
        except httpx.TransportError as error:
            meter.events.append(
                {
                    **event,
                    "error_class": type(error).__name__,
                    "latency_ms": round((time.monotonic() - started) * 1000),
                }
            )
            meter.transport_error = True
            raise
        try:
            body = response.json()
        except ValueError:
            body = {}
        usage = body.get("usage") if isinstance(body, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        prompt = usage.get("input_tokens", usage.get("prompt_tokens"))
        estimated = not isinstance(prompt, int)
        if estimated:
            # Never under-meter: bill the full request size when usage is absent.
            prompt = len(request.content or b"") // 3
        cost = usage.get("cost")
        if not isinstance(cost, (int, float)) or isinstance(cost, bool):
            cost = prompt * JEV_INPUT_RATE / 1e6
        meter.extra_cost += float(cost)
        meter.events.append(
            {
                **event,
                "status": response.status_code,
                "latency_ms": round((time.monotonic() - started) * 1000),
                "input_tokens": prompt,
                "usage_estimated": estimated,
                "cost_usd": float(cost),
            }
        )
        if response.status_code >= 500 or response.status_code in {
            401,
            403,
            408,
            429,
        }:
            meter.transport_error = True
        if meter.cost + getattr(meter, "prior_cost", 0) >= meter.max_cost:
            response.close()
            meter.stop("cost_limit")
        return response

    def close(self):
        self.transport.close()


def make_evaluator_backend(name, meter, timeout_ms=15000):
    """Return None for the managed LLM evaluator or a metered standalone Jev client."""
    if name == "llm":
        return None
    if name != "jev":
        raise ValueError(f"Unknown evaluator backend {name!r}")
    api_key = os.environ.get("JEV_API_KEY")
    if not api_key:
        raise ValueError("The jev evaluator requires JEV_API_KEY in the environment")
    from intaris.config import JevConfig
    from intaris.jev import JevClient

    config = JevConfig(enabled=True, timeout_ms=timeout_ms)
    # JevConfig adopts the managed Cognis route whenever COGNIS_INFERENCE_URL is
    # set; the benchmark needs standalone OpenRouter Jev regardless.
    config.base_url = JEV_OPENROUTER_BASE_URL
    config.model = JEV_OPENROUTER_MODEL
    config.api_key = api_key
    return JevClient(config, transport=JevMeteredTransport(meter))


def snapshot_db(source, destination):
    fd = os.open(destination, os.O_CREAT | os.O_WRONLY, 0o600)
    os.fchmod(fd, 0o600)
    os.close(fd)
    # sqlite3's own context manager commits but does not close connections.
    with (
        closing(sqlite3.connect(source)) as src,
        closing(sqlite3.connect(destination)) as dst,
    ):
        src.backup(dst)


def run(options):
    if not options.approve_paid:
        raise ValueError(
            "Paid execution requires --approve-paid; preparation never runs inference"
        )
    if os.environ.get("EVALUATION_POLICY_FORCE"):
        raise ValueError("Paid RC3 replay requires EVALUATION_POLICY_FORCE unset")
    if not os.environ.get("COGNIS_INFERENCE_URL") or not os.environ.get(
        "COGNIS_INFERENCE_TOKEN_FILE"
    ):
        raise ValueError(
            "Configure managed endpoint and token file; direct paid providers are forbidden"
        )
    from openai import OpenAI

    from intaris.audit import AuditStore
    from intaris.config import DBConfig, JudgeConfig, LLMConfig
    from intaris.db import Database
    from intaris.evaluator import _CONTEXT_ARG_KEY, Evaluator
    from intaris.intention import IntentionBarrier
    from intaris.judge import JudgeReviewer
    from intaris.llm import LLMClient
    from intaris.session import SessionStore
    from intaris.task_context import TaskContext
    from tools.rc3_replay import shape

    prior = []
    resume = getattr(options, "resume_from", None)
    if resume:
        original = json.loads((resume / "paid-input.json").read_text())
        prior = [
            json.loads(line)
            for line in (resume / "results.jsonl").read_text().splitlines()
        ]
        seen = {r["call_id"] for r in prior}
        if (
            len(prior) != 20
            or len(seen) != 20
            or len(original) != 300
            or len({r["call_id"] for r in original}) != 300
        ):
            raise ValueError(
                "Authorized continuation requires original 300 and exactly 20 attempted IDs"
            )
        if not seen <= {r["call_id"] for r in original}:
            raise ValueError("Prior IDs are not in original input")
        dataset = [r for r in original if r["call_id"] not in seen]
        if len(dataset) != 280:
            raise ValueError("Continuation must contain exactly 280 untouched IDs")
        options.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    else:
        dataset = json.loads((options.output / "paid-input.json").read_text())
    if len({r["call_id"] for r in dataset}) != len(dataset):
        raise ValueError("Duplicate input call IDs")
    # Exclusive creation prevents accidental paid batch reruns.
    if resume:
        reservation = os.open(
            resume / "continuation-reserved.json",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(reservation, "w") as stream:
            json.dump(
                {
                    "output": str(options.output.resolve()),
                    "call_ids": [r["call_id"] for r in dataset],
                },
                stream,
            )
            stream.flush()
            os.fsync(stream.fileno())
    journal = os.open(
        options.output / "results.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    os.fchmod(journal, 0o600)
    meter = Meter(
        getattr(options, "input_rate", 0.15),
        getattr(options, "output_rate", 0.60),
        getattr(options, "max_cost", 5.0),
    )
    meter.prior_cost = sum(row.get("cost_usd", 0) for row in prior)
    if resume:
        private_write(options.output / "paid-input.json", dataset)
    results = []
    clients = []
    jev = make_evaluator_backend(getattr(options, "evaluator", "llm"), meter)

    def client_factory(*args, **kwargs):
        if str(kwargs.get("base_url", "")).rstrip("/") != os.environ[
            "COGNIS_INFERENCE_URL"
        ].rstrip("/"):
            meter.stop("non_managed_route")
        kwargs.update(
            http_client=httpx.Client(transport=MeteredTransport(meter)), max_retries=0
        )
        client = OpenAI(*args, **kwargs)
        clients.append(client)
        return client

    with (
        os.fdopen(journal, "w") as stream,
        tempfile.TemporaryDirectory(prefix="intaris-paid-replay-") as directory,
        ExitStack() as resources,
        patch("intaris.llm.OpenAI", side_effect=client_factory),
    ):
        llm = LLMClient(LLMConfig(model="routes/intaris_evaluation"))
        analysis = LLMClient(
            LLMConfig(model="routes/intaris_analysis", timeout_ms=30000)
        )
        judge_llm = LLMClient(LLMConfig(model="routes/intaris_judge", timeout_ms=30000))
        source_db = Path(directory) / "replay.db"
        db = resources.enter_context(
            closing(Database(DBConfig(backend="sqlite", path=str(source_db))))
        )
        sessions = SessionStore(db)
        audit = AuditStore(db)
        for row in dataset:
            sid = row["call_id"]
            start = time.monotonic()
            before = meter.prompt, meter.completion, meter.requests, meter.cost
            events_before = len(getattr(meter, "events", []))
            entry = {
                "call_id": sid,
                "tool": row["tool"],
                "stratum": row.get("stratum", "unknown"),
                "legacy_decision": "approve",
                "args_shape": shape(row["args"]),
            }
            try:
                policy = {
                    k: v
                    for k, v in row["policy"].items()
                    if k
                    not in {
                        "active_policy",
                        "policy_hash",
                        "policy_revision",
                        "compiler_revision",
                        "source_fingerprint",
                    }
                }
                policy.update(
                    evaluation_policy_version="hybrid_v1",
                    profile=getattr(options, "profile", "balanced"),
                )
                sessions.create(
                    session_id=sid,
                    user_id="local-paid-replay",
                    intention=row["intention"][:500],
                    policy=policy,
                    details=row["details"],
                )
                if row.get("original_assignment") is not None:
                    with db.cursor() as cur:
                        cur.execute(
                            "UPDATE sessions SET original_assignment=? "
                            "WHERE user_id=? AND session_id=?",
                            (row["original_assignment"], "local-paid-replay", sid),
                        )
                for action in row.get("recent_actions", []):
                    audit.insert(
                        call_id=str(uuid.uuid4()),
                        user_id="local-paid-replay",
                        session_id=sid,
                        agent_id=None,
                        tool=action["tool"],
                        args_redacted={"path": action["target"]},
                        classification=None,
                        decision="escalate",
                        risk=None,
                        reasoning=None,
                        latency_ms=0,
                        evaluation_path="replay_history",
                    )
                for message in row["user_messages"]:
                    audit.insert(
                        call_id=str(uuid.uuid4()),
                        user_id="local-paid-replay",
                        session_id=sid,
                        agent_id=None,
                        record_type="reasoning",
                        tool=None,
                        args_redacted={"context": {"role": "user"}},
                        content=message["content"],
                        classification=None,
                        decision="approve",
                        risk=None,
                        reasoning=None,
                        latency_ms=0,
                        evaluation_path="reasoning",
                    )

                async def generate_profile():
                    owner = IntentionBarrier(
                        db=db, llm=analysis, generation="single", timeout_ms=300000
                    )
                    await owner.trigger("local-paid-replay", sid)
                    await owner.wait("local-paid-replay", sid)
                    return (
                        owner.update_count,
                        owner.recovery_attempts,
                        owner.update_errors,
                    )

                updates, retries, errors = asyncio.run(generate_profile())
                entry.update(profile_updates=updates, profile_retries=retries)
                entry["profile_errors"] = errors
                context = TaskContext(
                    llm=analysis,
                    sessions=sessions,
                    audit=audit,
                    generation="single",
                    extractor_identity={"model": "managed-replay"},
                )
                evaluator = Evaluator(
                    llm=llm,
                    session_store=sessions,
                    audit_store=audit,
                    task_context=context,
                    jev=jev,
                )
                reviewer = JudgeReviewer(
                    llm=judge_llm,
                    config=JudgeConfig(mode=getattr(options, "judge_mode", "advisory")),
                    audit_store=audit,
                    session_store=sessions,
                    evaluator=evaluator,
                )
                args = dict(row["args"])
                request_context = args.pop(_CONTEXT_ARG_KEY, {})
                if args.get("context") == request_context:
                    args.pop("context", None)
                result = evaluator.evaluate(
                    user_id="local-paid-replay",
                    session_id=sid,
                    agent_id=None,
                    tool=row["tool"],
                    args=args,
                    context=request_context,
                )
                entry["pre_judge_decision"] = result["decision"]
                entry["judge_invoked"] = bool(
                    reviewer.is_enabled
                    and result.get("evaluation_metadata", {}).get("judge_required")
                )
                if entry["judge_invoked"]:
                    asyncio.run(
                        reviewer.review_and_resolve(
                            user_id="local-paid-replay",
                            session_id=sid,
                            agent_id=None,
                            call_id=result["call_id"],
                        )
                    )
                saved = audit.get_by_call_id(
                    result["call_id"], user_id="local-paid-replay"
                )
                metadata = saved.get("evaluation_metadata") or {}
                entry.update(
                    audit_call_id=result["call_id"],
                    decision=saved.get("user_decision") or saved["decision"],
                    reason_codes=list(
                        filter(
                            None,
                            [
                                saved.get("outcome_override"),
                                result.get("outcome_override"),
                                (
                                    metadata.get("final_policy_result")
                                    or metadata.get("policy_result")
                                    or {}
                                ).get("override"),
                                metadata.get("policy_result", {}).get(
                                    "permission_basis"
                                ),
                                (
                                    metadata.get("policy_evidence", {}).get("review")
                                    or {}
                                ).get("basis"),
                            ],
                        )
                    ),
                    explanation=saved.get("judge_reasoning")
                    or saved.get("reasoning")
                    or result.get("reasoning"),
                )
            except ReplayStop as error:
                entry.update(decision="stopped", reason_codes=[str(error)])
            except Exception as error:
                if error.__cause__ is not None:
                    entry["cause_type"] = type(error.__cause__).__name__
                    validator = getattr(error.__cause__, "validator", None)
                    if isinstance(validator, str):
                        entry["validation_keyword"] = validator
                entry.update(
                    decision="error",
                    error_type=type(error).__name__,
                    reason_codes=[getattr(error, "code", None) or type(error).__name__],
                )
            finally:
                entry.update(
                    latency_ms=round((time.monotonic() - start) * 1000, 2),
                    inference_events=getattr(meter, "events", [])[events_before:],
                    inference_requests=meter.requests - before[2],
                    prompt_tokens=meter.prompt - before[0],
                    completion_tokens=meter.completion - before[1],
                    cost_usd=meter.cost - before[3],
                    cumulative_cost_usd=meter.cost + meter.prior_cost,
                    transport_error=meter.transport_error,
                )
                meter.finish()
                results.append(entry)
                stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
                snapshot_db(source_db, options.output / "audit.db")
                private_write(options.output / "paid-results.json", results)
            if meter.stop_reason:
                break
        for client in clients:
            client.close()
        if jev is not None:
            jev.close()
    counts = Counter(r.get("decision", "error") for r in results)
    strata = {}
    for r in results:
        strata.setdefault(r["stratum"], Counter())[r["decision"]] += 1
    latencies = sorted(r["latency_ms"] for r in results)
    private_write(
        options.output / "paid-report.json",
        {
            "counts": dict(counts),
            "strata": {key: dict(value) for key, value in strata.items()},
            "attempted": len(results),
            "stop_reason": meter.stop_reason,
            "inference_requests": meter.requests,
            "prompt_tokens": meter.prompt,
            "completion_tokens": meter.completion,
            "cost_usd": meter.cost,
            "evaluator": getattr(options, "evaluator", "llm"),
            "judge_mode": getattr(options, "judge_mode", "advisory"),
            "jev_cost_usd": meter.extra_cost,
            "input_rate_per_million": meter.input_rate,
            "output_rate_per_million": meter.output_rate,
            "transport_error_calls": meter.transport_calls,
            "latency_p50_ms": latencies[len(latencies) // 2] if latencies else None,
            "latency_p95_ms": latencies[
                min(len(latencies) - 1, int(len(latencies) * 0.95))
            ]
            if latencies
            else None,
            "legacy_approved": len(dataset),
            "approve_agreement_percent": round(
                100 * counts["approve"] / len(results), 2
            )
            if results
            else None,
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["prepare", "estimate", "run", "enrich-history"]
    )
    parser.add_argument("--source", type=Path)
    parser.add_argument(
        "--export", type=Path, default=Path("/tmp/intaris-rc3-replay/export.json")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("/tmp/intaris-rc3-replay/paid")
    )
    parser.add_argument("--approve-paid", action="store_true")
    parser.add_argument("--resume-from", type=Path)
    parser.add_argument("--input-rate", type=float, default=0.15)
    parser.add_argument("--output-rate", type=float, default=0.60)
    parser.add_argument(
        "--profile",
        choices=["strict", "balanced", "bounded_autonomy"],
        default="balanced",
    )
    parser.add_argument("--max-cost", type=float, default=5.0)
    parser.add_argument(
        "--judge-mode", choices=["advisory", "disabled"], default="advisory"
    )
    parser.add_argument("--evaluator", choices=["llm", "jev"], default="llm")
    options = parser.parse_args()
    options.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(options.output, 0o700)
    if options.action == "enrich-history":
        if options.source is None:
            parser.error("--source is required for enrich-history")
        enrich_history(options.source, options.output)
    elif options.action == "estimate":
        estimate = estimate_cost(
            json.loads((options.output / "paid-input.json").read_text()),
            options.input_rate,
            options.output_rate,
        )
        private_write(options.output / "paid-estimate.json", estimate)
        print(json.dumps(estimate))
    else:
        (prepare if options.action == "prepare" else run)(options)


if __name__ == "__main__":
    main()
