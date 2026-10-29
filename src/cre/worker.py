"""Database-polling background worker."""

import functools
import logging
import os
import signal
import socket
import threading
from collections.abc import Callable
from types import FrameType
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from cre import investigation_worker
from cre.config import get_settings
from cre.db import create_db_engine
from cre.llm import get_provider
from cre.logging_setup import configure_logging
from cre.telemetry import configure_tracing

logger = logging.getLogger("cre.worker")

CLAIM_SQL = text(
    "SELECT id, kind FROM jobs WHERE status = 'pending' ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED"
)
MARK_DONE_SQL = text(
    "UPDATE jobs SET status = 'done', attempts = attempts + 1, updated_at = now() "
    "WHERE id = :job_id"
)
MARK_FAILED_SQL = text(
    "UPDATE jobs SET status = 'failed', last_error = :last_error, updated_at = now() "
    "WHERE id = :job_id"
)


def process_one(conn: Connection) -> int | None:
    row = conn.execute(CLAIM_SQL).first()
    if row is None:
        return None
    job_id, kind = int(row[0]), str(row[1])
    if kind == "noop":
        conn.execute(MARK_DONE_SQL, {"job_id": job_id})
        status = "done"
    else:
        conn.execute(MARK_FAILED_SQL, {"job_id": job_id, "last_error": "unknown job kind"})
        status = "failed"
    logger.info(
        "job_processed",
        extra={"event": "job_processed", "job_id": job_id, "kind": kind, "status": status},
    )
    return job_id


def run(
    engine: Engine,
    poll_seconds: float,
    stop: threading.Event,
    max_iterations: int | None = None,
    *,
    process_investigation: Callable[[Engine], UUID | None] | None = None,
) -> int:
    processed = 0
    iterations = 0
    while not stop.is_set() and (max_iterations is None or iterations < max_iterations):
        iterations += 1
        try:
            if process_investigation is not None and process_investigation(engine) is not None:
                processed += 1
                continue
            with engine.begin() as conn:
                job_id = process_one(conn)
        except DBAPIError:
            logger.error(
                "worker_iteration_failed",
                exc_info=True,
                extra={"event": "worker_iteration_failed"},
            )
            stop.wait(poll_seconds)
            continue
        if job_id is None:
            stop.wait(poll_seconds)
        else:
            processed += 1
    return processed


def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    configure_tracing(settings)
    engine = create_db_engine(settings.database_url)
    stop = threading.Event()

    def _handle_signal(signum: int, _frame: FrameType | None) -> None:
        logger.info("worker_stopping", extra={"event": "worker_stopping", "signal": signum})
        stop.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    logger.info("worker_started", extra={"event": "worker_started"})
    run(
        engine,
        settings.worker_poll_seconds,
        stop,
        process_investigation=functools.partial(
            investigation_worker.process_investigation,
            settings=settings,
            provider=get_provider(settings),
            worker_id=f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}",
        ),
    )


if __name__ == "__main__":
    main()
