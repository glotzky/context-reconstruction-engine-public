"""FastAPI service for the incident lab."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Path
from fastapi.responses import JSONResponse
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.trace import Status, StatusCode
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import DBAPIError

from cre.config import Settings, get_settings
from cre.db import create_db_engine, get_connection
from cre.evidence_api import router as evidence_router
from cre.ingest import router as ingest_router
from cre.investigate_api import router as investigate_router
from cre.investigation_api import router as investigation_router
from cre.logging_setup import configure_logging
from cre.orders import Order, fetch_order
from cre.report import build_demo_source
from cre.report_api import router as report_router
from cre.request_id import RequestIDMiddleware, request_id_var
from cre.telemetry import configure_tracing

logger = logging.getLogger("cre.api")


def create_app(settings: Settings | None = None, *, engine: Engine | None = None) -> FastAPI:
    settings = settings if settings is not None else get_settings()
    configure_logging(settings)
    provider = configure_tracing(settings)
    if engine is None and settings.database_url:
        engine = create_db_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        logger.info("service_started", extra={"event": "service_started"})
        yield

    app = FastAPI(
        title="Context Reconstruction Engine lab", version=settings.version, lifespan=lifespan
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.report_source = build_demo_source(settings) if settings.report_demo else None
    app.add_middleware(RequestIDMiddleware)
    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
    app.include_router(ingest_router)
    app.include_router(evidence_router)
    app.include_router(investigation_router)
    app.include_router(report_router)
    app.include_router(investigate_router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {
            "status": "ok",
            "service": settings.service_name,
            "version": settings.version,
            "commit": settings.commit,
        }

    @app.get("/orders/{order_id}", response_model=Order)
    def get_order(
        order_id: Annotated[int, Path(ge=1)],
        conn: Annotated[Connection, Depends(get_connection)],
    ) -> Order | JSONResponse:
        try:
            order = fetch_order(conn, order_id)
        except DBAPIError as e:
            span = trace.get_current_span()
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, type(e.orig).__name__))
            logger.error(
                "order_lookup_failed",
                exc_info=True,
                extra={
                    "event": "order_lookup_failed",
                    "order_id": order_id,
                    "sql_error": str(e.orig),
                },
            )
            return JSONResponse(
                status_code=500,
                content={
                    "error": "database_error",
                    "error_type": type(e.orig).__name__,
                    "request_id": request_id_var.get(),
                },
            )
        if order is None:
            raise HTTPException(status_code=404, detail="order not found")
        return order

    return app


app = create_app()
