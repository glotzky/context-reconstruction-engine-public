"""Database engine and FastAPI connection dependency."""

from collections.abc import Iterator

from fastapi import HTTPException, Request
from sqlalchemy import Connection, Engine, create_engine


def create_db_engine(database_url: str) -> Engine:
    if not database_url:
        raise ValueError("database_url must not be empty")
    return create_engine(database_url, pool_pre_ping=True, hide_parameters=True)


def get_connection(request: Request) -> Iterator[Connection]:
    engine: Engine | None = request.app.state.engine
    if engine is None:
        raise HTTPException(status_code=503, detail="database not configured")
    with engine.connect() as conn:
        yield conn
