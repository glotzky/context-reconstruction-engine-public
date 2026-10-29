"""Human-run only: upgrades to head and back to base on a disposable Compose PostgreSQL DB."""

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from cre.config import Settings
from cre.db import create_db_engine

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL is not set"),
]

ROOT = Path(__file__).resolve().parents[2]
TABLES = {"orders", "jobs", "evidence_items", "investigation_jobs", "investigation_steps"}


def test_upgrade_head_then_downgrade_base_is_clean() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    engine = create_db_engine(Settings().database_url)
    try:
        command.downgrade(config, "base")
        command.upgrade(config, "head")
        inspector = inspect(engine)
        assert TABLES <= set(inspector.get_table_names())
        assert "customer_name" in {col["name"] for col in inspector.get_columns("orders")}

        command.downgrade(config, "base")
        assert TABLES.isdisjoint(inspect(engine).get_table_names())
    finally:
        engine.dispose()
        command.downgrade(config, "base")
