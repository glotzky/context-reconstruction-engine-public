"""Human-run only: applies migrations and the schema-rename lab failure to a disposable DB."""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from cre import lab
from cre.api import create_app
from cre.config import Settings
from cre.db import create_db_engine

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL is not set"),
]

ROOT = Path(__file__).resolve().parents[2]


def test_trigger_and_reset_schema_rename() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    engine = create_db_engine(Settings().database_url)
    try:
        command.downgrade(config, "base")
        command.upgrade(config, "head")
        with TestClient(create_app(Settings())) as client:
            ok = client.get("/orders/1")
            assert ok.status_code == 200
            assert ok.json()["customer_name"] == "Test Customer One"

            with engine.begin() as conn:
                assert lab.apply(conn, "schema-rename") is True
            broken = client.get("/orders/1")
            assert broken.status_code == 500
            assert broken.json()["error_type"] == "UndefinedColumn"

            with engine.begin() as conn:
                assert lab.reset(conn) == ["schema-rename"]
            assert client.get("/orders/1").status_code == 200

            with engine.begin() as conn:
                assert lab.revert(conn, "schema-rename") is False
    finally:
        engine.dispose()
        command.downgrade(config, "base")
