"""Accounted transport for explicitly granted fixed-model Cognis benchmarks."""

import json
import secrets
import threading
import time
from urllib.parse import unquote

import httpx

from tools.benchmark.release_proxy import Proxy, continued_budget


class DirectProxy(Proxy):
    MODEL_IDS = {
        "routes/intaris_evaluation": "providers/groq/openai/gpt-oss-20b",
        "routes/intaris_analysis": "providers/groq/openai/gpt-oss-120b",
        "routes/intaris_judge": "providers/groq/openai/gpt-oss-120b",
    }

    def __init__(
        self,
        output,
        api_key,
        previous_budget,
        *,
        base_url,
        transport=None,
        reviewed_transport_restart=None,
    ):
        self.output = output
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        self.context = {}
        self.records = []
        self.validation_errors = []
        self.budget = continued_budget(
            previous_budget, reviewed_transport_restart=reviewed_transport_restart
        )
        self.process = None
        self.client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": "Bearer " + api_key},
            timeout=45,
            follow_redirects=False,
            transport=transport,
        )
        try:
            self.recover()
            self.models = self.exchange({"path": "/models"})
            self.start_server()
        except BaseException:
            self.close()
            raise

    def model_for(self, stage):
        return "openai/gpt-oss-20b" if stage == "l1" else "openai/gpt-oss-120b"

    def recover(self, *, deadline=None):
        deadline = min(deadline or float("inf"), time.monotonic() + 180)
        while time.monotonic() + 15 < deadline:
            try:
                response = self.client.get("/models", timeout=10)
                response.raise_for_status()
                rows = {row["id"]: row for row in response.json()["data"]}
                binding, models = {}, []
                for route, model in self.MODEL_IDS.items():
                    row = rows[model]
                    if row.get("owned_by") != "groq":
                        self.budget.stop_reason = "proxy_route_changed"
                        raise ValueError("Fixed provider/model identity changed")
                    binding[route] = {
                        "provider_id": "groq",
                        "model": model.removeprefix("providers/groq/"),
                        "resolution_fingerprint": row["resolution_fingerprint"],
                    }
                    models.append({**row, "id": route})
                self.binding, self.local_models = binding, models
                with (self.output / "direct-model-binding.jsonl").open("a") as stream:
                    stream.write(json.dumps(binding) + "\n")
                return
            except (KeyError, ValueError):
                self.budget.stop_reason = "proxy_route_changed"
                raise
            except (httpx.HTTPError, OSError):
                time.sleep(2)
        self.budget.stop_reason = "proxy_recovery_exhausted"
        raise RuntimeError("Direct metadata recovery exhausted")

    def exchange(self, request, timeout=55):
        if "body" not in request:
            path = unquote(request["path"].split("?", 1)[0])
            if path == "/models":
                return {"status": 200, "body": {"data": self.local_models}}
            model = path.removeprefix("/models/")
            row = next((r for r in self.local_models if r["id"] == model), None)
            return {"status": 200 if row else 404, "body": row or {}}
        body = dict(request["body"])
        route = body["model"]
        body["model"] = self.MODEL_IDS[route]
        body["reasoning_effort"] = (
            "medium" if route == "routes/intaris_judge" else "low"
        )
        started = time.monotonic()
        response = self.client.post(
            "/chat/completions", json=body, timeout=min(timeout, 45)
        )
        return {
            "status": response.status_code,
            "body": response.json(),
            "headers": {"retry-after": response.headers.get("retry-after", "")},
            "proxy_ms": (time.monotonic() - started) * 1000,
        }

    def checked_metadata(self):
        self.recover()
        return self.exchange({"path": "/models"})

    def close(self):
        if hasattr(self, "server"):
            self.server.shutdown()
            self.server.server_close()
        self.client.close()
