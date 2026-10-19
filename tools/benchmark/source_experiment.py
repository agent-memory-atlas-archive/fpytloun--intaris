"""Runnable paired primary/history entry point, gated on explicit paid approval."""

import argparse
import json
from pathlib import Path

from tools.benchmark.matched import secret_file, write_json
from tools.benchmark.provider_recorder import ProviderRecorder
from tools.benchmark.source_budget import CumulativeBudget
from tools.benchmark.source_context import execute, verify
from tools.benchmark.source_history_runner import execute_history, plan
from tools.benchmark.source_local_factory import LocalFactory
from tools.benchmark.source_price import global_bound
from tools.benchmark.source_scope import attempt_cap


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in (
        "runtime",
        "manifest",
        "scope",
        "proof",
        "output",
        "groq-key",
        "jev-key",
    ):
        parser.add_argument("--" + key, type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--explicit-paid-authorization", action="store_true")
    args = parser.parse_args()
    if not args.explicit_paid_authorization:
        raise ValueError(
            "No credential reads or providers without explicit paid authorization"
        )
    manifest, scope, proof = (
        json.loads(p.read_text()) for p in (args.manifest, args.scope, args.proof)
    )
    verify(manifest)
    cap = attempt_cap(scope, manifest["sha256"])
    if proof.get("approved_revision") != args.revision:
        raise ValueError("Evidence revision mismatch")
    budget = CumulativeBudget(reserve_usd=global_bound(), max_attempts=cap)
    with ProviderRecorder(
        secret_file(args.groq_key),
        secret_file(args.jev_key),
        max_requests=cap,
        budget=budget,
        forbid_compiler=True,
        extraction_schema_name="task_context_extraction",
    ) as relay:
        factory = LocalFactory(
            runtime=args.runtime,
            revision=args.revision,
            directory=args.output / "private-runtime",
            relay=relay,
            proof=proof,
        )
        try:
            execute(manifest, factory, relay, args.output / "primary", scope=scope)
            execute_history(
                plan(manifest),
                primary_manifest=manifest,
                factories=factory,
                relay=relay,
                output=args.output / "history",
                scope=scope,
            )
        finally:
            factory.close()
            write_json(args.output / "cumulative-budget.json", budget.snapshot())


if __name__ == "__main__":
    main()
