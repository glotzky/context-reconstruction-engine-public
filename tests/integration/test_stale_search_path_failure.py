"""Human-run only: the stale-search-path lab failure against a disposable DB."""

import os
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import DBAPIError

from cre import lab
from cre.api import create_app
from cre.config import Settings
from cre.db import create_db_engine
from cre.evidence_repo import evidence_table
from cre.orders import ORDER_QUERY

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL is not set"),
]

ROOT = Path(__file__).resolve().parents[2]


def _config() -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    return config


def _broken_response(client: TestClient) -> tuple[int, dict[str, Any]]:
    response = client.get("/orders/1")
    body = response.json()
    body.pop("request_id", None)
    return response.status_code, body


def _primary_message() -> str:
    engine = create_db_engine(Settings().database_url)
    try:
        with engine.connect() as conn, pytest.raises(DBAPIError) as info:
            conn.execute(ORDER_QUERY, {"order_id": 1})
        message: str = info.value.orig.diag.message_primary  # type: ignore[union-attr]
        return message
    finally:
        engine.dispose()


def _reset_on_fresh_engine() -> None:
    engine = create_db_engine(Settings().database_url)
    try:
        with engine.begin() as conn:
            lab.reset(conn)
    finally:
        engine.dispose()


def _apply(engine: Engine, name: str) -> None:
    with engine.begin() as conn:
        assert lab.apply(conn, name) is True


def test_both_failures_same_status_body_and_error_type() -> None:
    config = _config()
    engine = create_db_engine(Settings().database_url)
    try:
        command.downgrade(config, "base")
        command.upgrade(config, "head")
        with TestClient(create_app(Settings())) as client:
            assert client.get("/orders/1").status_code == 200

            _apply(engine, "schema-rename")
            rename_status, rename_body = _broken_response(client)
            rename_message = _primary_message()
            with engine.begin() as conn:
                assert lab.reset(conn) == ["schema-rename"]

            _apply(engine, "stale-search-path")
            client.app.state.engine.dispose()  # type: ignore[attr-defined]
            stale_status, stale_body = _broken_response(client)
            stale_message = _primary_message()

        assert rename_status == stale_status == 500
        assert rename_body == stale_body
        assert stale_body["error_type"] == "UndefinedColumn"
        assert rename_message == stale_message
        assert 'column "customer_name" does not exist' in stale_message
    finally:
        engine.dispose()
        _reset_on_fresh_engine()
        command.downgrade(config, "base")


def test_stale_search_path_records_event_and_reverts() -> None:
    config = _config()
    engine = create_db_engine(Settings().database_url)
    try:
        command.downgrade(config, "base")
        command.upgrade(config, "head")
        with TestClient(create_app(Settings())) as client:
            _apply(engine, "stale-search-path")
            with engine.connect() as conn:
                rows = conn.execute(select(evidence_table.c.kind, evidence_table.c.observed)).all()
            assert [row.kind for row in rows] == ["config_change"]
            assert rows[0].observed["key"] == "search_path"

            with engine.begin() as conn, pytest.raises(lab.LabStateError):
                lab.apply(conn, "schema-rename")
            with engine.begin() as conn:
                assert lab.reset(conn) == ["stale-search-path"]
            assert client.get("/orders/1").status_code == 200

        with engine.connect() as conn:
            stale = conn.execute(
                text("SELECT count(*) FROM pg_namespace WHERE nspname = 'lab_stale'")
            ).scalar_one()
        assert stale == 0
        with engine.begin() as conn:
            assert lab.revert(conn, "stale-search-path") is False
    finally:
        engine.dispose()
        _reset_on_fresh_engine()
        command.downgrade(config, "base")
