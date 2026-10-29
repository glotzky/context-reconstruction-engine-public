import copy
import uuid
from typing import Any

import pytest

from cre.evidence import EVIDENCE_ADAPTER
from cre.redaction import REDACTED, is_sensitive_key, redact, redact_evidence, redact_text
from tests.evidence_samples import RETRIEVED_AT, SPAN_ID, TRACE_ID


@pytest.mark.parametrize(
    "secret",
    [
        "AKIAABCDEFGHIJKLMNOP",
        "sk-fakefakefakefake1234",
        "ghp_" + "a1" * 12,
        "xoxb-1234-fake-token-value",
    ],
)
def test_keys_redacted(secret: str) -> None:
    result = redact_text(f"leaked {secret} here")
    assert secret not in result
    assert result == f"leaked {REDACTED} here"


def test_bearer_token_redacted_keeping_word() -> None:
    result = redact_text("Authorization: Bearer abc.def-ghi_123")
    assert "abc.def-ghi_123" not in result
    assert "Bearer" in result
    assert result.endswith(f"Bearer {REDACTED}")


def test_jwt_redacted() -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmYWtlIn0.c2lnbmF0dXJl"
    result = redact_text(f"cookie {jwt}")
    assert jwt not in result and REDACTED in result


@pytest.mark.parametrize(
    ("text", "key", "value"),
    [
        ("password=hunter2-fake", "password=", "hunter2-fake"),
        ('{"password": "fake pass"}', '"password": ', "fake pass"),
        ("secret: fake-secret-value", "secret: ", "fake-secret-value"),
        ("url?token=faketoken123&x=1", "token=", "faketoken123"),
        ("api_key=fakeapikey", "api_key=", "fakeapikey"),
        ("API-KEY=fakeapikey", "API-KEY=", "fakeapikey"),
    ],
)
def test_key_value_secrets_redacted(text: str, key: str, value: str) -> None:
    result = redact_text(text)
    assert value not in result
    assert key in result
    assert REDACTED in result


def test_postgres_url_password_redacted_keeping_user_and_host() -> None:
    result = redact_text("postgresql+psycopg://lab:lab-password@db:5432/lab")
    assert result == f"postgresql+psycopg://lab:{REDACTED}@db:5432/lab"


def test_redis_url_redacted() -> None:
    result = redact_text("cache at redis://default:fake-redis-pw@cache:6379/0")
    assert "fake-redis-pw" not in result
    assert f"redis://default:{REDACTED}@cache:6379/0" in result


def test_sensitive_dict_keys_fully_replaced() -> None:
    data = {
        "password": "plain",
        "Authorization": "Basic whatever",
        "db.connection_string": "host=db",
        "api-key": 12345,
        "token": None,
        "user": "fake-user",
    }
    result = redact(data)
    assert result == {
        "password": REDACTED,
        "Authorization": REDACTED,
        "db.connection_string": REDACTED,
        "api-key": REDACTED,
        "token": None,
        "user": "fake-user",
    }


def test_nested_recursion_and_types() -> None:
    data: dict[str, Any] = {
        "outer": [{"inner": {"secret_value": "x"}}, "password=deep-fake"],
        "pair": ("redis://u:fake-pw@h:1", 3),
        "count": 7,
        "flag": True,
    }
    result = redact(data)
    assert result["outer"][0]["inner"]["secret_value"] == REDACTED
    assert result["outer"][1] == f"password={REDACTED}"
    assert isinstance(result["pair"], tuple)
    assert result["pair"] == (f"redis://u:{REDACTED}@h:1", 3)
    assert result["count"] == 7 and result["flag"] is True


def test_input_not_mutated() -> None:
    data = {"password": "plain", "nested": {"msg": "token=abc123fake"}, "items": ["sk-" + "a" * 20]}
    original = copy.deepcopy(data)
    redact(data)
    assert data == original


@pytest.mark.parametrize(
    "text",
    [
        "order_lookup_failed for order 1",
        TRACE_ID,
        SPAN_ID,
        str(uuid.UUID("12345678-1234-5678-1234-567812345678")),
        "2026-01-01T00:00:05.123Z",
        "token expired; please retry",
    ],
)
def test_no_false_positives(text: str) -> None:
    assert redact_text(text) == text


def test_redact_evidence_returns_redacted_item_with_same_id() -> None:
    item = EVIDENCE_ADAPTER.validate_python(
        {
            "kind": "log",
            "timestamp": "2026-01-01T00:00:05Z",
            "provenance": {
                "source": {"source_type": "structured_log", "name": "fake"},
                "retrieval_method": "structured_log_json",
                "identifiers": {"content_sha256": "ab" * 32},
                "retrieved_at": RETRIEVED_AT,
            },
            "observed": {
                "level": "ERROR",
                "message": "connect failed password=fake-msg-pw",
                "attributes": {"dsn_hint": "x", "detail": "postgresql://u:fake-attr-pw@db/x"},
            },
        }
    )
    redacted = redact_evidence(item)
    assert redacted.id == item.id
    assert redacted is not item
    assert redacted.kind == "log"
    dumped = redacted.model_dump_json()
    assert "fake-msg-pw" not in dumped and "fake-attr-pw" not in dumped
    assert redacted.observed.attributes["dsn_hint"] == REDACTED
    assert "fake-msg-pw" in item.model_dump_json()


@pytest.mark.parametrize(
    "text",
    [
        "login password=fake-pw-1 ok",
        "token: fake-token-2",
        '{"secret": "fake-secret-3"}',
        "api_key='fake-key-4'",
        "postgresql://lab:fake-pw-5@db:5432/lab",
        "Authorization: Bearer fake.bearer-6",
    ],
)
def test_redact_text_idempotent(text: str) -> None:
    once = redact_text(text)
    assert REDACTED in once
    assert redact_text(once) == once


def test_redact_text_bare_value_not_corrupted_on_second_pass() -> None:
    assert redact_text(redact_text("auth failed token=abc")) == f"auth failed token={REDACTED}"


def test_redact_evidence_idempotent() -> None:
    item = EVIDENCE_ADAPTER.validate_python(
        {
            "kind": "log",
            "timestamp": "2026-01-01T00:00:05Z",
            "provenance": {
                "source": {"source_type": "structured_log", "name": "fake"},
                "retrieval_method": "structured_log_json",
                "identifiers": {"content_sha256": "ab" * 32},
                "retrieved_at": RETRIEVED_AT,
            },
            "observed": {
                "level": "ERROR",
                "message": "auth failed password=fake-pw token: fake-tok",
                "attributes": {"note": "api_key=fake-key", "password": "fake"},
            },
        }
    )
    once = redact_evidence(item)
    assert "fake-pw" not in once.model_dump_json()
    assert redact_evidence(once) == once


@pytest.mark.parametrize(
    ("key", "expected"),
    [("DB_PASSWORD", True), ("api-key", True), ("Database.Url", True), ("db.pool_size", False)],
)
def test_is_sensitive_key_public(key: str, expected: bool) -> None:
    assert is_sensitive_key(key) is expected
