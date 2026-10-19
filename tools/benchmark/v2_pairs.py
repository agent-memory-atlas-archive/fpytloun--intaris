"""Compiler-free real-model structured contrasts; no internal Evidence input."""

from __future__ import annotations

import argparse
import json
import random
import secrets
from pathlib import Path

from tools.benchmark.client import IntarisClient
from tools.benchmark.hybrid_suite import verify_hybrid_rows
from tools.benchmark.matched import (
    Runtime,
    harness_sources,
    replay_scenario,
    secret_file,
    write_json,
)
from tools.benchmark.provider_recorder import ProviderRecorder
from tools.benchmark.v2_comparison import assert_compiler_free, assess_contrast, verify


def scenario(pair: dict, variant: dict) -> dict:
    """Serialize only the pre-frozen public request and structured policy."""
    request = pair["request"]
    return {
        "id": pair["id"] + "/" + variant["id"],
        "cohort": "v2_structured_pairs",
        "split": "separate_policy_contrast",
        "intention": pair["intention"],
        "details": pair["details"],
        "policy": variant["policy"],
        "steps": [
            {
                "id": pair["id"] + "/" + variant["id"],
                "label": None,
                "tool": request["tool"],
                "args": request["args"],
                "context": request["context"],
            }
        ],
    }


def run(args) -> None:
    manifest = json.loads(args.manifest.read_text())
    verify(manifest)
    if not args.ready_runtime_revision or args.ready_runtime_revision != args.revision:
        raise ValueError("Main-approved reviewed runtime revision required")
    if args.wire_approved_manifest_hash != manifest["sha256"]:
        raise ValueError("Main-approved exact structured wire manifest required")
    args.output.mkdir(exist_ok=False, parents=True)
    rows = []
    info = {
        "manifest_sha256": manifest["sha256"],
        "runtime_revision": args.revision,
        "harness_sources": harness_sources(),
        "completed": False,
        "evidence_kind": "real_model_structured_policy_contrasts",
        "compiler_expected": 0,
        "planned_rows": len(manifest["pairs"])
        * 2
        * manifest["trials"]
        * len(args.arms),
    }
    write_json(args.output / "manifest.json", manifest)
    try:
        with ProviderRecorder(
            secret_file(args.groq_key),
            secret_file(args.jev_key),
            forbid_compiler=True,
        ) as relay:
            runtimes = {}
            try:
                for arm in args.arms:
                    runtimes[arm] = Runtime(
                        args.runtime,
                        args.revision,
                        args.output / ("runtime-" + arm),
                        relay,
                        arm,
                        args.judge,
                        mode="hybrid_v1",
                    )
                for trial in range(1, manifest["trials"] + 1):
                    rnd = random.Random(manifest["seed"] + trial)
                    pairs = list(manifest["pairs"])
                    rnd.shuffle(pairs)
                    for pair in pairs:
                        arms = list(args.arms)
                        rnd.shuffle(arms)
                        for arm in arms:
                            variants = list(pair["variants"])
                            rnd.shuffle(variants)
                            for variant in variants:
                                relay.context = {
                                    "trial": trial,
                                    "pair": pair["id"],
                                    "variant": variant["id"],
                                    "arm": arm,
                                }
                                runtime = runtimes[arm]
                                with IntarisClient(
                                    runtime.url,
                                    runtime.api_key,
                                    "v2-pair-" + secrets.token_hex(12),
                                    "benchmark",
                                    timeout_s=60,
                                    request_retries=0,
                                ) as client:
                                    batch = replay_scenario(
                                        scenario(pair, variant),
                                        client,
                                        relay,
                                        arm="v2_" + arm,
                                        trial=trial,
                                        judge=args.judge,
                                    )
                                verify_hybrid_rows(batch)
                                row = {
                                    "pair": pair["id"],
                                    "variant": variant["id"],
                                    "arm": arm,
                                    "trial": trial,
                                    "judge": args.judge,
                                    "calls": batch,
                                    "assessment": assess_contrast(
                                        batch[0], pair, variant["id"]
                                    ),
                                }
                                rows.append(row)
                                with (args.output / "results.jsonl").open(
                                    "a"
                                ) as stream:
                                    stream.write(json.dumps(row, sort_keys=True) + "\n")
                                assert_compiler_free(
                                    relay.records, relay.validation_errors
                                )
                                if relay.stopped:
                                    raise RuntimeError(
                                        "Provider budget or outage; failed rows retained"
                                    )
                info["completed"] = True
            finally:
                for runtime in runtimes.values():
                    runtime.close()
                relay.drain()
                write_json(args.output / "provider-attempts.json", relay.records)
                write_json(
                    args.output / "provider-validation-errors.json",
                    relay.validation_errors,
                )
    finally:
        info["recorded_rows"] = len(rows)
        info["missing_rows"] = info["planned_rows"] - len(rows)
        write_json(args.output / "run.json", info)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--ready-runtime-revision", required=True)
    parser.add_argument("--wire-approved-manifest-hash", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--groq-key", type=Path, required=True)
    parser.add_argument("--jev-key", type=Path, required=True)
    parser.add_argument(
        "--arms", nargs="+", choices=["llm", "jev"], default=["llm", "jev"]
    )
    parser.add_argument(
        "--judge", choices=["disabled", "advisory", "auto"], default="disabled"
    )
    run(parser.parse_args())


if __name__ == "__main__":
    main()
