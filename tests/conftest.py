"""Shared fixtures for analysis queue tests."""

import os

import pytest

from intaris.config import DBConfig
from intaris.db import Database


@pytest.fixture(params=["sqlite", "postgresql"])
def queue_db(request, tmp_path):
    config = DBConfig()
    config.backend = request.param
    config.path = str(tmp_path / "queue.db")
    if request.param == "postgresql":
        config.database_url = os.environ.get("INTARIS_QUEUE_TEST_DSN", "")
        if not config.database_url:
            pytest.skip("INTARIS_QUEUE_TEST_DSN must name a disposable database")
    db = Database(config)
    with db.cursor() as cur:
        cur.execute("DELETE FROM analysis_tasks")
    yield db
    db.close()
