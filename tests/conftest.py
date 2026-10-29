import io
import json
import logging
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cre.api import create_app
from cre.config import Settings
from cre.db import get_connection
from cre.logging_setup import JsonFormatter

FAKE_DATABASE_URL = "postgresql+psycopg://fake_user:fake_password@localhost:5432/fake_db"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        DATABASE_URL=FAKE_DATABASE_URL,
        CRE_SERVICE_NAME="cre-test",
        CRE_VERSION="9.9.9-test",
        CRE_COMMIT="deadbeef",
        CRE_LOG_LEVEL="INFO",
        CRE_OTEL_EXPORTER="none",
    )


@pytest.fixture
def fake_conn() -> MagicMock:
    return MagicMock(name="connection")


@pytest.fixture
def app(settings: Settings, fake_conn: MagicMock) -> FastAPI:
    application = create_app(settings)

    def _fake_connection() -> Iterator[MagicMock]:
        yield fake_conn

    application.dependency_overrides[get_connection] = _fake_connection
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


class LogCapture:
    def __init__(self, stream: io.StringIO) -> None:
        self.stream = stream

    def records(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.stream.getvalue().splitlines() if line]


@pytest.fixture
def log_capture(settings: Settings) -> Iterator[LogCapture]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter(settings.service_name, settings.version, settings.commit))
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield LogCapture(stream)
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)
