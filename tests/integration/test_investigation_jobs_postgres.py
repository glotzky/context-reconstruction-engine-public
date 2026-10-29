"""Human-run only: a worker is killed after ``round:1`` and a second worker resumes the job."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from cre.api import create_app
from cre.config import Settings
from cre.evidence_repo import store_evidence
from cre.investigation_worker import process_investigation
from cre.llm import FakeProvider
from cre.prompts import HYPOTHESIS_SYSTEM_PROMPT
from tests.evidence_samples import TRACE_ID
from tests.integration._investigation_child import ANCHOR, EARLIER_LOG, INITIAL_LOG

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL is not set"),
]

ROOT = Path(__file__).resolve().parents[2]
CONCLUDE = '{"action": "conclude", "tool_calls": [], "reason": "fake done"}'


def _wait_for_step(engine: object, job_id: UUID, step_key: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with engine.connect() as conn:  # type: ignore[attr-defined]
            found = conn.execute(
                text("SELECT 1 FROM investigation_steps WHERE job_id = :id AND step_key = :key"),
                {"id": job_id, "key": step_key},
            ).first()
        if found is not None:
            return
        time.sleep(0.2)
    raise AssertionError(f"step {step_key} was not persisted in time")


def test_killed_worker_is_resumed_without_duplicate_steps() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    engine = create_engine(os.environ["DATABASE_URL"])
    child: subprocess.Popen[bytes] | None = None
    try:
        command.downgrade(config, "base")
        command.upgrade(config, "0005_investigation_jobs")
        with engine.begin() as conn:
            store_evidence(conn, INITIAL_LOG)
            store_evidence(conn, EARLIER_LOG)
        with TestClient(create_app(Settings())) as client:
            response = client.post(
                "/investigations", json={"timestamp": ANCHOR, "trace_id": TRACE_ID}
            )
        assert response.status_code == 202
        job_id = UUID(response.json()["id"])

        child = subprocess.Popen(
            [sys.executable, "-m", "tests.integration._investigation_child", "worker-child"],
            cwd=ROOT,
        )
        _wait_for_step(engine, job_id, "round:1")
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=10)

        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE investigation_jobs SET lease_expires_at = now() - interval '1 second' "
                    "WHERE id = :id"
                ),
                {"id": job_id},
            )
        provider = FakeProvider([CONCLUDE])
        assert process_investigation(engine, Settings(), provider, "worker-resume") == job_id

        with engine.connect() as conn:
            status, attempts, result = conn.execute(
                text("SELECT status, attempts, result FROM investigation_jobs WHERE id = :id"),
                {"id": job_id},
            ).one()
            keys = (
                conn.execute(
                    text(
                        "SELECT step_key FROM investigation_steps WHERE job_id = :id ORDER BY seq"
                    ),
                    {"id": job_id},
                )
                .scalars()
                .all()
            )
        assert status == "completed" and attempts == 2
        assert result["stop_reason"] == "concluded"
        assert keys == ["collect:trace", "hypotheses:initial", "round:1"]
        assert len(provider.requests) == 1
        assert provider.requests[0].system != HYPOTHESIS_SYSTEM_PROMPT
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        engine.dispose()
        command.downgrade(config, "base")
