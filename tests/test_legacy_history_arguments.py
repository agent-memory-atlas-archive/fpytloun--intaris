import pytest

from tools.benchmark.source_history_runner import historical_arguments_match


def test_supplemental_selection_retains_full_denominator():
    from tools.benchmark.paired_release import completeness, select_jobs

    jobs = [
        {"lane": "focused", "case_id": case, "trial": trial, "packet": {"steps": [{}]}}
        for case in (
            "applicable-human-record",
            "unrelated-human-record",
            "request-revoked",
        )
        for trial in (1, 2, 3)
    ]
    selected, metadata = select_jobs(
        jobs, ["applicable-human-record", "unrelated-human-record"]
    )
    assert metadata["supplemental"]
    assert metadata["full_corpus_rows_per_mode"] == 9
    assert metadata["planned_rows_per_mode"] == 6
    assert completeness(selected, [], None, ("legacy",))["planned_rows"] == 6
    assert select_jobs(jobs)[1]["planned_rows_per_mode"] == 9
    with pytest.raises(ValueError, match="Unknown selected cases"):
        select_jobs(jobs, ["missing-case"])


def test_only_duplicated_legacy_server_context_is_excluded():
    expected = {"to": "release@internal.invalid", "body": "Build passed"}
    envelope = {"minimum_outcome": "escalate"}
    actual = {**expected, "context": envelope, "__intaris_context": envelope}
    assert historical_arguments_match(actual, expected, legacy=True)
    assert not historical_arguments_match(actual, expected)
    assert "context" in actual  # Raw evidence remains untouched.
    assert not historical_arguments_match(
        {**actual, "to": "another@internal.invalid"}, expected, legacy=True
    )
    assert not historical_arguments_match(
        {**actual, "extra": True}, expected, legacy=True
    )
    assert not historical_arguments_match(
        {**actual, "context": {"different": True}}, expected, legacy=True
    )
    assert not historical_arguments_match(
        actual, {**expected, "context": {"caller": True}}, legacy=True
    )
    assert not historical_arguments_match(
        {**expected, "context": envelope}, expected, legacy=True
    )
    assert historical_arguments_match(expected, expected, legacy=True)
