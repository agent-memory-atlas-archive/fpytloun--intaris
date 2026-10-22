"""Recovered envelopes retain sequence identity without inventing chronology."""

import json

import pytest

from intaris.config import EventStoreConfig
from intaris.events.backend import FilesystemEventBackend, event_matches_filters
from intaris.events.store import EventStore


@pytest.mark.parametrize("timestamp", [None, "", 0, {}, "not-a-timestamp"])
@pytest.mark.parametrize("bound", ["after_ts", "before_ts"])
def test_unknown_time_is_preserved_but_cannot_match_time_bounds(timestamp, bound):
    event = {"ts": timestamp, "seq": 1, "type": "message", "source": None}
    assert event_matches_filters(event)
    assert not event_matches_filters(event, **{bound: "2026-01-02T00:00:00Z"})
    assert not event_matches_filters(event, sources={"client"})


def test_missing_timestamp_is_not_treated_as_earliest_time():
    assert event_matches_filters({"seq": 1})
    assert not event_matches_filters({"seq": 1}, before_ts="2026-01-02T00:00:00Z")


def test_backend_and_buffer_share_time_filter_and_preserve_canonical_history(tmp_path):
    config = EventStoreConfig(
        backend="filesystem",
        filesystem_path=str(tmp_path),
        flush_size=1000,
        flush_interval=3600,
    )
    backend = FilesystemEventBackend(config)
    recovered = [
        {"seq": 1, "ts": None, "source": None, "type": "message", "data": {}},
        {
            "seq": 2,
            "ts": "2026-01-01T00:00:00Z",
            "source": "client",
            "type": "message",
            "data": {},
        },
    ]
    backend.append("alice", "recovered", recovered)
    store = EventStore(config)
    try:
        assert [e["seq"] for e in store.read("alice", "recovered")] == [1, 2]
        store.append(
            "alice",
            "recovered",
            [
                {
                    "ts": None,
                    "type": "message",
                    "data": {"role": "assistant", "content": "Recovered"},
                }
            ],
        )
        # The persisted and still-buffered null timestamps must both be excluded.
        result = store.read(
            "alice", "recovered", before_ts="2026-01-02T00:00:00Z", limit=10
        )
        assert [e["seq"] for e in result] == [2]
        assert len(store.read("alice", "recovered")) == 3
        store.flush_session("alice", "recovered")
        assert len(store.read_tail("alice", "recovered", 10)) == 3
        assert json.loads(json.dumps(recovered[0]))["ts"] is None
    finally:
        store.flush_all()
