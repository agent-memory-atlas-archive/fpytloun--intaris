"""Tests for benchmark evaluator provenance."""

from __future__ import annotations

from tools.benchmark.models import RunConfig
from tools.benchmark.runner import ScenarioRunner


class _ConfigClient:
    def server_config(self) -> dict:
        return {
            "version": "0.10.1",
            "evaluator": {
                "backend": "jev",
                "model": "jev-1.13.0",
                "timeout_ms": 4000,
                "minimum_confidence": 0.6,
            },
        }


def test_run_metadata_captures_evaluator_provenance(tmp_path) -> None:
    runner = ScenarioRunner(
        RunConfig(
            output_dir=str(tmp_path),
            intaris_url="http://127.0.0.1:8061",
        )
    )
    runner._intaris = _ConfigClient()  # type: ignore[assignment]

    runner._save_run_meta(["session-benchmark-metadata"])
    metadata = runner.store.load_run_meta()

    assert metadata["intaris_config"]["version"] == "0.10.1"
    assert metadata["intaris_config"]["evaluator"] == {
        "backend": "jev",
        "model": "jev-1.13.0",
        "timeout_ms": 4000,
        "minimum_confidence": 0.6,
    }
