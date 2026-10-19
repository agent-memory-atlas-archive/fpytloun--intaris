"""Isolated assessment-only release run using existing Cognis companion routes.

Provider keys never leave the Intaris pod. A single persistent stdin/stdout bridge
forwards model requests only; fixture tool commands are never executed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import secrets
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from tools.benchmark.client import IntarisClient
from tools.benchmark.matched import freeze, replay_scenario, summarize, unused_port
from tools.benchmark.provider_recorder import safe_usage
from tools.benchmark.proxy_recovery import (
    BindingChanged,
    discover_unchanged_binding,
    refresh_fingerprints,
)
from tools.benchmark.source_budget import CumulativeBudget
from tools.benchmark.source_price import global_bound, request_bound

# No credentials, model-route edits, production session writes or tool execution.
# The SDK's own retries are disabled: every forwarded attempt is accounted here.
BRIDGE = """
import json,sys,time
from intaris.config import load_config
from intaris.llm import LLMClient
client=LLMClient(load_config().llm)._client.with_options(max_retries=0,base_url=sys.argv[1])
for line in sys.stdin:
    request=json.loads(line)
    started=time.monotonic()
    try:
        if request['path'].startswith('/models'):
            value=client.get(request['path'],cast_to=dict,options={'timeout':10})
        else:
            models=client.get('/models',cast_to=dict,options={'timeout':10})
            current=next(m for m in models['data'] if m['id']==request['body']['model'])
            if current['resolution_fingerprint']!=request['fingerprint']:
                print(json.dumps({'status':409,'body':{'error':{'code':'route_changed'}},
                                  'proxy_ms':0}),flush=True)
                continue
            value=client.post(request['path'],body=request['body'],cast_to=dict,
                              options={'timeout':40})
        answer={'status':200,'body':value}
    except Exception as exc:
        body=getattr(exc,'body',None)
        error=body.get('error',body) if isinstance(body,dict) else {}
        safe={k:v for k,v in error.items()
              if k in {'message','type','code','param','failed_generation'} and isinstance(v,(str,int))}
        # Only structured API fields, never request headers/credentials.
        answer={'status':getattr(exc,'status_code',502),
                'body':{'error':safe or {'type':type(exc).__name__}}}
        response=getattr(exc,'response',None)
        if response is not None:
            answer['headers']={k:v for k,v in response.headers.items()
                               if k.lower()=='retry-after'}
    answer['proxy_ms']=(time.monotonic()-started)*1000
    print(json.dumps(answer),flush=True)
"""

ROUTES = {
    "/intention/chat/completions": ("intention", "routes/intaris_analysis"),
    "/judge/chat/completions": ("judge", "routes/intaris_judge"),
    "/jev/v1/systemone": ("l1", "routes/intaris_evaluation"),
    "/v1/systemone": ("l1", "routes/intaris_evaluation"),
}


def load_previous_completion(
    path: str | None, *, reviewed_transport_restart: str | None = None
) -> dict:
    if not path:
        return {}
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict) or not isinstance(value.get("budget"), dict):
        raise ValueError("Previous completion has no valid budget")
    continued_budget(
        value["budget"], reviewed_transport_restart=reviewed_transport_restart
    )
    return value


def continued_budget(
    previous: dict | None, *, reviewed_transport_restart: str | None = None
) -> CumulativeBudget:
    approved_cap = int(os.environ.get("INTARIS_BENCHMARK_APPROVED_ATTEMPT_CAP", "2000"))
    if approved_cap not in (2000, 4000, 6000):
        raise ValueError("Unsupported approved attempt cap")
    if previous is None:
        return CumulativeBudget(
            reserve_usd=global_bound(), max_attempts=2000, max_usd=10
        )
    required = {
        "max_attempts",
        "attempts",
        "max_usd",
        "known_or_estimated_usd",
        "unknown_reserved_usd",
        "active_reservation_usd",
    }
    if not isinstance(previous, dict) or not required <= previous.keys():
        raise ValueError("Incomplete continuation budget")
    restart_epoch = (
        bool(
            reviewed_transport_restart
            and reviewed_transport_restart.startswith(
                ("verified_replica_restart:", "verified_request_correction:")
            )
            and reviewed_transport_restart.partition(":")[2].strip()
        )
        and previous.get("stop_reason")
        in {"three_consecutive_upstream_failures", "proxy_recovery_exhausted"}
        and type(previous.get("consecutive_failures")) is int
        and 0 <= previous["consecutive_failures"] <= 3
    )
    transport_reviewed = restart_epoch or (
        bool(reviewed_transport_restart and reviewed_transport_restart.strip())
        and previous.get("stop_reason") in (None, "proxy_bridge_failed")
        and type(previous.get("consecutive_failures")) is int
        and 0 <= previous["consecutive_failures"] < 3
    )
    if (
        previous.get("stop_reason") or previous.get("consecutive_failures", 0)
    ) and not transport_reviewed:
        raise ValueError("Stopped or failing continuation requires explicit review")
    for key in required:
        value = previous[key]
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError("Invalid continuation budget")
    if (
        type(previous["max_attempts"]) is not int
        or type(previous["attempts"]) is not int
        or previous["max_attempts"] > approved_cap
        or previous["max_usd"] > 10
    ):
        raise ValueError("Continuation exceeds original approved cap")
    attempts = previous["max_attempts"] - previous["attempts"]
    dollars = previous["max_usd"] - sum(
        previous[key]
        for key in (
            "known_or_estimated_usd",
            "unknown_reserved_usd",
            "active_reservation_usd",
        )
    )
    if attempts <= 0 or dollars < global_bound():
        raise ValueError("Cumulative benchmark budget exhausted")
    return CumulativeBudget(
        reserve_usd=global_bound(),
        max_attempts=attempts,
        max_usd=dollars,
        initial_failures=0
        if restart_epoch
        else previous.get("consecutive_failures", 0),
    )


def startup_failure_budget(previous: dict | None) -> dict:
    # A failed continuation must never erase already spent/reserved budget.
    return previous if previous is not None else continued_budget(None).snapshot()


def verified_proxy_url(binding: dict) -> str:
    """Discover the allowed existing pod; never export auth to a caller-selected host."""
    namespace = os.environ["INTARIS_BENCHMARK_NAMESPACE"]
    pod_name = os.environ["INTARIS_BENCHMARK_COGNIS_POD"]
    pod = json.loads(
        subprocess.check_output(
            [
                "kubectl",
                "-n",
                namespace,
                "get",
                "pod",
                pod_name,
                "-o",
                "json",
            ],
            text=True,
        )
    )
    port = next(
        p["containerPort"]
        for p in pod["spec"]["containers"][0]["ports"]
        if p["name"] == "http"
    )
    url = f"http://{pod['status']['podIP']}:{port}/v1"
    expected = binding["_transport"]
    if pod["metadata"]["uid"] != expected["pod_uid"] or url != expected["base_url"]:
        raise ValueError("Pinned Cognis replica changed")
    return url


class Proxy:
    def __init__(
        self,
        output: Path,
        binding: dict,
        previous_budget: dict | None = None,
        *,
        reviewed_transport_restart: str | None = None,
    ):
        self.output = output
        self.binding = json.loads(json.dumps(binding))
        self.records: list[dict] = []
        self.context: dict = {}
        self.token = secrets.token_urlsafe(32)
        self.budget = continued_budget(
            previous_budget,
            reviewed_transport_restart=reviewed_transport_restart,
        )
        self.lock = threading.Lock()
        self.reader = ThreadPoolExecutor(max_workers=1)
        self.process = None
        try:
            try:
                proxy_url = verified_proxy_url(binding)
            except ValueError:
                self.recover()
                proxy_url = None
            if proxy_url is not None:
                self.start_bridge(proxy_url)
            self.models = self.checked_metadata()
        except BaseException:
            self.close()
            raise
        if self.models["status"] != 200:
            self.close()
            raise RuntimeError("Companion metadata discovery failed")
        ids = {r["id"] for r in self.models["body"]["data"]}
        if not {route for _, route in ROUTES.values()} <= ids:
            self.close()
            raise RuntimeError("Required companion route unavailable")
        self.start_server()

    def model_for(self, stage):
        return "typesafe/jev-1.13-20260917" if stage == "l1" else "openai/gpt-oss-120b"

    def start_server(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                if self.headers.get("Authorization") != f"Bearer {owner.token}":
                    self.send_error(403)
                    return
                path, separator, query = self.path.partition("?")
                prefixes = (
                    "/intention/models",
                    "/judge/models",
                    "/jev/v1/models",
                    "/v1/models",
                )
                prefix = next(
                    (p for p in prefixes if path == p or path.startswith(p + "/")),
                    None,
                )
                if prefix is None:
                    self.send_error(403)
                    return
                with owner.lock:
                    try:
                        request = {
                            "path": "/models"
                            + path[len(prefix) :]
                            + (separator + query)
                        }
                        try:
                            answer = owner.exchange(request)
                            if answer["status"] in (429, 502, 503, 504):
                                raise RuntimeError("Metadata transport unavailable")
                        except Exception:
                            owner.recover()
                            answer = owner.exchange(request)
                    except Exception:
                        owner.budget.stop_reason = "proxy_metadata_bridge_failed"
                        answer = {
                            "status": 503,
                            "body": {"error": {"code": "metadata_unavailable"}},
                        }
                self.reply(answer)

            def reply(self, answer):
                data = json.dumps(answer["body"]).encode()
                self.send_response(answer["status"])
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in answer.get("headers", {}).items():
                    if k.lower() == "retry-after":
                        self.send_header(k, v)
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_POST(self):
                if self.headers.get(
                    "Authorization"
                ) != f"Bearer {owner.token}" or self.path not in {
                    *ROUTES,
                    "/v1/chat/completions",
                }:
                    self.send_error(403)
                    return
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 1_000_000:
                    self.send_error(413)
                    return
                body = json.loads(self.rfile.read(size))
                if self.path == "/v1/chat/completions":
                    route = body.get("model")
                    stages = {
                        "routes/intaris_evaluation": "l1",
                        "routes/intaris_analysis": "intention",
                        "routes/intaris_judge": "judge",
                    }
                    if route not in stages:
                        self.send_error(403)
                        return
                    stage = stages[route]
                else:
                    stage, route = ROUTES[self.path]
                if body.get("stream"):
                    self.send_error(400)
                    return
                priced_model = owner.model_for(stage)
                try:
                    request_bound(priced_model, body)
                except ValueError:
                    self.send_error(400)
                    return
                body["model"] = route
                with owner.lock:
                    answer = owner.forward(stage, route, body)
                self.reply(answer)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def start_bridge(self, proxy_url):
        namespace = os.environ["INTARIS_BENCHMARK_NAMESPACE"]
        deployment = os.environ["INTARIS_BENCHMARK_DEPLOYMENT"]
        self.process = subprocess.Popen(
            [
                "kubectl",
                "-n",
                namespace,
                "exec",
                "-i",
                f"deployment/{deployment}",
                "--",
                "python",
                "-u",
                "-c",
                BRIDGE,
                proxy_url,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    def stop_bridge(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process = None

    def checked_metadata(self):
        """Same bounded verification/recovery for startup and final acceptance."""
        try:
            models = self.exchange({"path": "/models"}, timeout=12)
            if models.get("status") != 200:
                raise RuntimeError("Metadata unavailable")
            self.verify_binding(models, self.binding)
            return models
        except json.JSONDecodeError:
            raise
        except (RuntimeError, TimeoutError, OSError, KeyError, ValueError):
            self.recover()
            models = self.exchange({"path": "/models"}, timeout=12)
            self.verify_binding(models, self.binding)
            return models

    def recover(self, *, deadline=None):
        """Reconnect only after read-only verification; never reset the ledger."""
        deadline = min(deadline or float("inf"), time.monotonic() + 240)
        last = None
        delays = iter((1, 3, 8))
        # Reserve time for bounded discovery, metadata and bridge cleanup.
        while time.monotonic() + 80 < deadline:
            delay = next(delays, 15)
            time.sleep(delay)
            try:
                candidate = discover_unchanged_binding(self.binding)
                self.stop_bridge()
                self.start_bridge(candidate["_transport"]["base_url"])
                models = self.exchange({"path": "/models"}, timeout=12)
                refresh_fingerprints(candidate, models)
                self.verify_binding(models, candidate)
                # Check SQL identity again after discovery to bound a route race.
                check = discover_unchanged_binding(self.binding)
                if check["_transport"] != candidate["_transport"]:
                    raise RuntimeError("Replica changed during recovery")
                with (self.output / "transport-recovery.jsonl").open("a") as stream:
                    stream.write(
                        json.dumps(
                            {
                                "previous": self.binding,
                                "replacement": candidate,
                                "attempts_before_recovery": self.budget.snapshot()[
                                    "attempts"
                                ],
                            }
                        )
                        + "\n"
                    )
                self.binding = candidate
                return
            except (BindingChanged, ValueError):
                self.budget.stop_reason = "proxy_route_changed"
                raise
            except Exception as exc:
                last = exc
                with (self.output / "transport-recovery-errors.jsonl").open(
                    "a"
                ) as stream:
                    stream.write(
                        json.dumps(
                            {
                                "error_type": type(exc).__name__,
                                "attempts": self.budget.snapshot()["attempts"],
                            }
                        )
                        + "\n"
                    )
        self.budget.stop_reason = "proxy_recovery_exhausted"
        raise RuntimeError("Bounded transport recovery exhausted") from last

    def forward(self, stage, route, body):
        """Each retry is an independently reserved, durable provider attempt."""
        deadline = time.monotonic() + 480
        for retry in range(3):
            if time.monotonic() + 55 >= deadline:
                self.budget.stop_reason = "proxy_retry_deadline"
                return {"status": 503, "body": {"error": {"code": "retry_deadline"}}}
            attempt = self.budget.reserve()
            if attempt is None:
                return {"status": 503, "body": {"error": {"code": "budget_stopped"}}}
            record = {
                **self.context,
                "attempt": attempt,
                "retry": retry,
                "stage": stage,
                "provider": "jev"
                if self.model_for(stage).startswith("typesafe/")
                else "groq",
                "model": self.model_for(stage),
                "proxy_route": route,
            }
            started = time.monotonic()
            try:
                answer = self.exchange(
                    {
                        "path": "/systemone" if stage == "l1" else "/chat/completions",
                        "body": body,
                        "fingerprint": self.binding[route]["resolution_fingerprint"],
                    }
                )
                record.update(
                    status=answer["status"],
                    usage=safe_usage(answer["body"].get("usage")),
                    proxy_ms=answer.get("proxy_ms", 0),
                    error_code=answer["body"].get("error", {}).get("code")
                    if isinstance(answer["body"].get("error"), dict)
                    else None,
                )
            except Exception as exc:
                answer = {
                    "status": 502,
                    "body": {"error": {"code": "bridge_unavailable"}},
                }
                record.update(status=502, error=type(exc).__name__)
            record["elapsed_ms"] = (time.monotonic() - started) * 1000
            self.budget.finish(attempt, record)
            self.records.append(record)
            with (self.output / "provider-attempts.jsonl").open("a") as stream:
                stream.write(json.dumps(record) + "\n")
            (self.output / "budget.json").write_text(
                json.dumps(self.budget.snapshot(), indent=2)
            )
            if (
                answer["status"] not in (409, 429, 502, 503, 504)
                or retry == 2
                or self.stopped
            ):
                return answer
            try:
                # Revalidates route identity even for 409: fingerprints are
                # process-local and rotate during an otherwise identical rollout.
                # Leave 55s for inference, up to10s Retry-After and overhead.
                self.recover(deadline=deadline - 70)
                header = answer.get("headers", {}).get("retry-after", "0")
                try:
                    delay = min(10, max(0, float(header)))
                except (ValueError, TypeError):
                    delay = 1
                time.sleep(delay)
            except Exception:
                return {"status": 503, "body": {"error": {"code": "recovery_failed"}}}
        raise AssertionError("Unreachable retry loop")

    def exchange(self, payload, *, timeout=55):
        self.process.stdin.write(json.dumps(payload) + "\n")
        self.process.stdin.flush()
        # Replacement always uses the same ledger, not a fresh allowance.
        future = self.reader.submit(self.process.stdout.readline)
        try:
            line = future.result(timeout=timeout)
        except FutureTimeout:
            self.process.terminate()
            raise TimeoutError("Proxy bridge deadline")
        if not line:
            raise RuntimeError("Proxy bridge closed")
        return json.loads(line)

    @staticmethod
    def verify_binding(models, binding):
        expected = {
            "routes/intaris_evaluation": (
                "openrouter",
                "typesafe/jev-1.13",
                "systemone",
                32000,
                57600,
            ),
            "routes/intaris_analysis": (
                "groq",
                "openai/gpt-oss-120b",
                "chat",
                131072,
                65536,
            ),
            "routes/intaris_judge": (
                "groq",
                "openai/gpt-oss-120b",
                "chat",
                131072,
                65536,
            ),
        }
        rows = {r["id"]: r for r in models.get("body", {}).get("data", [])}
        for route, (provider, model, operation, inputs, outputs) in expected.items():
            observed = binding[route]
            row = rows[route]
            if (
                observed["provider_id"] != provider
                or observed["model"] != model
                or row["resolution_fingerprint"] != observed["resolution_fingerprint"]
                or operation not in row["cognis"]["operations"]
                or not 0 < row["max_input_tokens"] <= inputs
                or not 0 < row["max_output_tokens"] <= outputs
            ):
                raise ValueError("Unpriced or changed proxy resolution")

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    @property
    def stopped(self):
        return self.budget.snapshot()["stop_reason"] is not None

    def drain(self):
        with self.lock:
            pass

    def close(self):
        if hasattr(self, "server"):
            self.server.shutdown()
            self.server.server_close()
        self.stop_bridge()
        self.reader.shutdown(wait=True, cancel_futures=True)


def completeness(manifest, trials, rows, budget, error):
    planned = {
        (trial, scenario["id"], step["id"])
        for trial in range(1, trials + 1)
        for scenario in manifest["scenarios"]
        for step in scenario["steps"]
    }
    collected = {(r["trial"], r["scenario"], r["case"]) for r in rows}
    return {
        "planned_observations": len(planned),
        "collected_observations": len(collected),
        "missing_observations": [list(key) for key in sorted(planned - collected)],
        "collection_complete": not (planned - collected) and error is None,
        "error": error,
        "budget": budget,
        "uncollected_are_not_safe_outcomes": True,
    }


class RecordedClient(IntarisClient):
    def __init__(self, *args, evidence_path: Path, **kwargs):
        super().__init__(*args, **kwargs)
        self.evidence_path = evidence_path

    def create_session(self, *args, **kwargs):
        policy = dict(kwargs.get("policy") or {})
        policy.update(
            evaluation_policy_version="hybrid_v1",
            profile="balanced",
            interaction_mode="step_requests",
        )
        kwargs["policy"] = policy
        result = super().create_session(*args, **kwargs)
        observed = self.get_session(args[0])
        with self.evidence_path.open("a") as handle:
            handle.write(json.dumps(observed) + "\n")
        active = observed.get("policy") or {}
        if (
            active.get("evaluation_policy_version") != "hybrid_v1"
            or active.get("profile") != "balanced"
        ):
            raise RuntimeError("Session did not activate frozen hybrid balanced policy")
        return result


def run(args):
    revision = subprocess.check_output(
        ["git", "-C", args.runtime, "rev-parse", "HEAD"], text=True
    ).strip()
    if revision != args.revision:
        raise ValueError("Runtime revision mismatch")
    subprocess.run(
        ["git", "-C", args.runtime, "diff", "--exit-code", "HEAD", "--", "intaris"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    args.port = unused_port()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    os.chmod(output, 0o700)
    manifest = freeze(Path("tools/benchmark/fixtures/hybrid-real-world-cases.json"))
    # Keep historical fields only under a historical key, not as run settings.
    historical = {
        k: manifest.pop(k)
        for k in list(manifest)
        if k
        not in {
            "scenarios",
            "mapping",
            "corpus_sha256",
            "format",
            "assessment_only",
            "auto_resolve",
        }
    }
    manifest["historical_manifest_settings"] = historical
    binding = json.loads(Path(args.binding).read_text())
    previous_completion = load_previous_completion(args.previous_completion)
    manifest["release_run"] = {
        "runtime_revision": args.revision,
        "candidate_only": True,
        "backend": "auto (verified Jev route)",
        "judge": "advisory",
        "trials": args.trials,
        "transport": "serialized Cognis companion proxy via kubectl bridge",
        "latency_caveat": "Includes proxy and bridge overhead; not direct-provider latency.",
        "max_usd": 10,
        "max_provider_attempts": 2000,
        "historical_baselines_are_not_concurrent": True,
        "model_binding": binding,
        "effective_context": "source_context/single",
        "analysis_enabled": True,
        "whole_api_retries": 0,
        "effective_session_policy_evidence": "sessions.jsonl",
        "timeouts": "Pinned runtime defaults; bridge 55s; SDK transport 40s; client 90s",
        "post_run_route_binding_verification_required": True,
        "evaluation_policy_version": "hybrid_v1",
        "policy_profile": "balanced",
        "interaction_mode": "step_requests",
        "prior_run_accounting": previous_completion.get("budget", {}),
    }
    manifest["sha256"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode()
    ).hexdigest()
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2))
    rows = []
    error = None
    try:
        proxy = Proxy(output, binding, previous_completion.get("budget"))
    except BaseException as exc:
        (output / "completion.json").write_text(
            json.dumps(
                completeness(
                    manifest,
                    args.trials,
                    rows,
                    startup_failure_budget(previous_completion.get("budget")),
                    "startup:" + type(exc).__name__,
                ),
                indent=2,
            )
        )
        raise
    try:
        (output / "proxy-models.json").write_text(json.dumps(proxy.models, indent=2))
        for trial in range(1, args.trials + 1):
            if proxy.stopped:
                break
            data = output / f"trial-{trial}"
            data.mkdir()
            token = secrets.token_urlsafe(32)
            token_file = data / "local-proxy-token"
            token_file.write_text(proxy.token)
            token_file.chmod(0o600)
            # Isolated SQLite, no production DB, notifications, or inherited keys.
            env = {
                k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ
            }
            env.update(
                {
                    "PYTHONPATH": str(Path(args.runtime).resolve()),
                    "INTARIS_HOST": "127.0.0.1",
                    "INTARIS_PORT": str(args.port),
                    "INTARIS_API_KEY": token,
                    "DATA_DIR": str(data),
                    "DB_BACKEND": "sqlite",
                    "DB_PATH": str(data / "intaris.db"),
                    "METRICS_ENABLED": "false",
                    "INTARIS_SEARCH_ENABLED": "false",
                    "ANALYSIS_ENABLED": "true",
                    "ANALYSIS_INTERVAL_MINUTES": "1000000",
                    "SUMMARY_VOLUME_THRESHOLD": "1000000",
                    "HYBRID_CONTEXT_MODE": "source_context",
                    "SOURCE_CONTEXT_GENERATION": "single",
                    "RATE_LIMIT": "0",
                    "EVALUATOR_BACKEND": "auto",
                    "COGNIS_INFERENCE_URL": proxy.url + "/v1",
                    "COGNIS_INFERENCE_TOKEN_FILE": str(token_file),
                    "JUDGE_MODE": "advisory",
                    "JUDGE_NOTIFY_MODE": "never",
                    "LLM_BASE_URL": proxy.url + "/jev/v1",
                    "LLM_API_KEY": proxy.token,
                    "LLM_MODEL": "routes/intaris_evaluation",
                    "ANALYSIS_LLM_BASE_URL": proxy.url + "/intention",
                    "ANALYSIS_LLM_API_KEY": proxy.token,
                    "ANALYSIS_LLM_MODEL": "routes/intaris_analysis",
                    "ANALYSIS_LLM_REASONING_EFFORT": "low",
                    "JUDGE_LLM_BASE_URL": proxy.url + "/judge",
                    "JUDGE_LLM_API_KEY": proxy.token,
                    "JUDGE_LLM_MODEL": "routes/intaris_judge",
                    "JUDGE_LLM_REASONING_EFFORT": "medium",
                    "JEV_BASE_URL": proxy.url + "/jev",
                    "JEV_API_KEY": proxy.token,
                    "JEV_MODEL": "routes/intaris_evaluation",
                }
            )
            with (data / "server.log").open("w") as log:
                server = subprocess.Popen(
                    [sys.executable, "-m", "intaris.server"],
                    cwd=args.runtime,
                    env=env,
                    stdout=log,
                    stderr=log,
                )
                try:
                    for _ in range(100):
                        if server.poll() is not None:
                            raise RuntimeError("Isolated server failed")
                        try:
                            if (
                                httpx.get(
                                    f"http://127.0.0.1:{args.port}/health", timeout=1
                                ).status_code
                                == 200
                            ):
                                break
                        except httpx.HTTPError:
                            pass
                        time.sleep(0.2)
                    else:
                        raise RuntimeError("Isolated readiness timeout")
                    with RecordedClient(
                        f"http://127.0.0.1:{args.port}",
                        token,
                        "release-benchmark",
                        "release-benchmark",
                        timeout_s=90,
                        request_retries=0,
                        evidence_path=output / "sessions.jsonl",
                    ) as client:
                        for scenario in manifest["scenarios"]:
                            batch = replay_scenario(
                                scenario,
                                client,
                                proxy,
                                arm="release-current",
                                trial=trial,
                                judge="advisory",
                            )
                            rows.extend(batch)
                            with (output / "rows.jsonl").open("a") as handle:
                                for row in batch:
                                    handle.write(json.dumps(row) + "\n")
                            (output / "summary.json").write_text(
                                json.dumps(summarize(rows), indent=2)
                            )
                            print(
                                json.dumps(
                                    {
                                        "trial": trial,
                                        "scenario": scenario["id"],
                                        "rows": len(rows),
                                        "budget": proxy.budget.snapshot(),
                                    }
                                ),
                                flush=True,
                            )
                            if proxy.stopped:
                                break
                finally:
                    server.terminate()
                    try:
                        server.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait()
    except BaseException as exc:
        error = type(exc).__name__
        raise
    finally:
        proxy.close()
        (output / "completion.json").write_text(
            json.dumps(
                completeness(
                    manifest, args.trials, rows, proxy.budget.snapshot(), error
                ),
                indent=2,
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--binding", required=True)
    parser.add_argument("--previous-completion")
    parser.add_argument("--trials", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--port", type=int, default=18767)
    run(parser.parse_args())
