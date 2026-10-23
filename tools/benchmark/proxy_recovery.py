"""Read-only discovery for recovery of the benchmark's authenticated transport."""

import json
import os
import subprocess


class BindingChanged(ValueError):
    """Recovery must never silently change the inference experiment."""


ROUTES_QUERY = """
import asyncio,json,os
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
async def main():
    engine=create_async_engine(os.environ['DATABASE_URL'])
    async with engine.connect() as connection:
        await connection.execute(text('SET TRANSACTION READ ONLY'))
        rows=await connection.execute(text(
            "SELECT task_type,provider_id,model,config->>'reasoning_effort' AS reasoning_effort "
            "FROM model_routing WHERE owner_email=:owner_email "
            "AND task_type IN ('intaris_evaluation','intaris_analysis','intaris_judge')"
        ), {'owner_email': os.environ['INTARIS_BENCHMARK_OWNER_EMAIL']})
        print(json.dumps([dict(row) for row in rows.mappings()]))
    await engine.dispose()
asyncio.run(main())
"""


def discover_unchanged_binding(expected):
    """Only the fixed authorized pod can supply a replacement credential target."""
    namespace = os.environ["INTARIS_BENCHMARK_NAMESPACE"]
    pod_name = os.environ["INTARIS_BENCHMARK_COGNIS_POD"]
    owner_email = os.environ["INTARIS_BENCHMARK_OWNER_EMAIL"]
    if not owner_email.strip():
        raise ValueError("INTARIS_BENCHMARK_OWNER_EMAIL must not be blank")
    pod = json.loads(
        subprocess.check_output(
            ["kubectl", "-n", namespace, "get", "pod", pod_name, "-o", "json"],
            text=True,
            timeout=10,
        )
    )
    if not any(
        c["type"] == "Ready" and c["status"] == "True"
        for c in pod.get("status", {}).get("conditions", [])
    ):
        raise RuntimeError("Cognis replica not ready")
    port = next(
        p["containerPort"]
        for p in pod["spec"]["containers"][0]["ports"]
        if p["name"] == "http"
    )
    rows = json.loads(
        subprocess.check_output(
            [
                "kubectl",
                "-n",
                namespace,
                "exec",
                pod_name,
                "--",
                "env",
                f"INTARIS_BENCHMARK_OWNER_EMAIL={owner_email}",
                "python",
                "-c",
                ROUTES_QUERY,
            ],
            text=True,
            timeout=10,
        )
    )
    candidate = {}
    for row in rows:
        route = "routes/" + row["task_type"]
        if route not in expected or any(
            row.get(key) != expected[route].get(key)
            for key in ("provider_id", "model", "reasoning_effort")
        ):
            raise BindingChanged("Provider/model/reasoning route changed")
        candidate[route] = {**expected[route]}
    if set(candidate) != {k for k in expected if k.startswith("routes/")}:
        raise BindingChanged("Required inference route disappeared")
    candidate["_transport"] = {
        "pod_uid": pod["metadata"]["uid"],
        "base_url": f"http://{pod['status']['podIP']}:{port}/v1",
    }
    return candidate


def refresh_fingerprints(candidate, models):
    if models.get("status") != 200:
        raise RuntimeError("Replacement replica metadata unavailable")
    rows = {r["id"]: r for r in models["body"]["data"]}
    for route in candidate:
        if route.startswith("routes/"):
            candidate[route]["resolution_fingerprint"] = rows[route][
                "resolution_fingerprint"
            ]
    return candidate
