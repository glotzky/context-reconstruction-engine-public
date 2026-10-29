import io
import json
import logging
import re
import sys

from opentelemetry.sdk.trace import TracerProvider

from cre.config import Settings
from cre.logging_setup import JsonFormatter, _CreHandler, configure_logging
from cre.request_id import request_id_var

RESERVED_KEYS = {
    "timestamp",
    "level",
    "logger",
    "message",
    "service",
    "version",
    "commit",
    "request_id",
    "trace_id",
    "span_id",
}


def _format(record: logging.LogRecord) -> dict[str, object]:
    formatter = JsonFormatter(service="cre-test", version="9.9.9-test", commit="deadbeef")
    return json.loads(formatter.format(record))  # type: ignore[no-any-return]


def _record(msg: str = "hello", **extra: object) -> logging.LogRecord:
    logger = logging.getLogger("cre.test")
    return logger.makeRecord("cre.test", logging.INFO, __file__, 1, msg, (), None, extra=extra)


def test_output_is_json_with_reserved_keys() -> None:
    data = _format(_record())
    assert RESERVED_KEYS <= data.keys()
    assert data["message"] == "hello"
    assert data["level"] == "INFO"
    assert data["logger"] == "cre.test"
    assert data["service"] == "cre-test"
    assert data["version"] == "9.9.9-test"
    assert data["commit"] == "deadbeef"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", str(data["timestamp"]))


def test_ids_are_null_outside_request_and_span() -> None:
    data = _format(_record())
    assert data["request_id"] is None
    assert data["trace_id"] is None
    assert data["span_id"] is None


def test_ids_present_inside_request_and_span() -> None:
    tracer = TracerProvider().get_tracer("test")
    token = request_id_var.set("req-1")
    try:
        with tracer.start_as_current_span("span"):
            data = _format(_record())
    finally:
        request_id_var.reset(token)
    assert data["request_id"] == "req-1"
    assert re.fullmatch(r"[0-9a-f]{32}", str(data["trace_id"]))
    assert re.fullmatch(r"[0-9a-f]{16}", str(data["span_id"]))


def test_extra_fields_merge_without_overwriting_reserved() -> None:
    data = _format(_record(order_id=7, service="attacker", request_id="spoofed"))
    assert data["order_id"] == 7
    assert data["service"] == "cre-test"
    assert data["request_id"] is None


def test_exc_info_adds_error_type_and_traceback() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.getLogger("cre.test").makeRecord(
            "cre.test", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
        )
    data = _format(record)
    assert data["error_type"] == "ValueError"
    assert "ValueError: boom" in str(data["exc_info"])


def test_configure_logging_is_idempotent() -> None:
    settings = Settings(CRE_LOG_LEVEL="DEBUG")
    root = logging.getLogger()
    original_level = root.level
    try:
        configure_logging(settings)
        configure_logging(settings)
        ours = [h for h in root.handlers if isinstance(h, _CreHandler)]
        assert len(ours) == 1
        assert isinstance(ours[0].formatter, JsonFormatter)
        assert root.level == logging.DEBUG
    finally:
        root.setLevel(original_level)


def test_configure_logging_writes_json_to_stdout(capsys: object) -> None:
    configure_logging(Settings(CRE_SERVICE_NAME="cre-stdout"))
    logging.getLogger("cre.test").warning("to_stdout")
    out = capsys.readouterr().out  # type: ignore[attr-defined]
    lines = [json.loads(line) for line in out.splitlines() if "to_stdout" in line]
    assert lines and lines[0]["service"] == "cre-stdout"


def test_stream_handler_stays_untouched() -> None:
    root = logging.getLogger()
    foreign = logging.StreamHandler(io.StringIO())
    root.addHandler(foreign)
    try:
        configure_logging(Settings())
        assert foreign in root.handlers
    finally:
        root.removeHandler(foreign)
