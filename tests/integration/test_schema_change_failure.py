"""Human-run only: applies migrations against a disposable Compose PostgreSQL database."""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from cre.api import create_app
from cre.config import Settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL is not set"),
]

ROOT = Path(__file__).resolve().parents[2]


def test_rename_migration_breaks_order_lookup() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    try:
        command.downgrade(config, "base")
        command.upgrade(config, "0001_baseline")
        with TestClient(create_app(Settings())) as client:
            ok = client.get("/orders/1")
            assert ok.status_code == 200
            assert ok.json()["customer_name"] == "Test Customer One"

            command.upgrade(config, "0002_rename_customer_name")
            broken = client.get("/orders/1")
            assert broken.status_code == 500
            assert broken.json()["error_type"] == "UndefinedColumn"
    finally:
        command.downgrade(config, "base")
