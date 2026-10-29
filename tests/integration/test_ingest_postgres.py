"""Human-run only: posts a log batch twice against a disposable Compose PostgreSQL database."""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from cre.api import create_app
from cre.config import Settings
from tests.evidence_samples import SAMPLE_LOG

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL is not set"),
]

ROOT = Path(__file__).resolve().parents[2]


def test_duplicate_log_batch_stores_one_row() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    try:
        command.downgrade(config, "base")
        command.upgrade(config, "0004_evidence_idempotency_key")
        payload = {"records": [SAMPLE_LOG]}
        with TestClient(create_app(Settings())) as client:
            first = client.post("/ingest/logs", json=payload)
            second = client.post("/ingest/logs", json=payload)
        assert first.status_code == 200 and first.json()["accepted"] == 1
        assert second.status_code == 200 and second.json()["duplicates"] == 1
        engine = create_engine(os.environ["DATABASE_URL"])
        try:
            with engine.connect() as conn:
                count = conn.execute(text("SELECT count(*) FROM evidence_items")).scalar_one()
        finally:
            engine.dispose()
        assert count == 1
    finally:
        command.downgrade(config, "base")
