import pytest
from pydantic import ValidationError

import cre
from cre.config import Settings, get_settings

ENV_VARS = [
    "DATABASE_URL",
    "CRE_SERVICE_NAME",
    "CRE_VERSION",
    "CRE_COMMIT",
    "CRE_LOG_LEVEL",
    "CRE_OTEL_EXPORTER",
    "CRE_WORKER_POLL_SECONDS",
    "CRE_INGEST_MAX_BODY_BYTES",
    "CRE_INGEST_MAX_ITEMS",
    "CRE_RETRIEVAL_DEFAULT_WINDOW_SECONDS",
    "CRE_RETRIEVAL_MAX_WINDOW_SECONDS",
    "CRE_RETRIEVAL_DEFAULT_LIMIT",
    "CRE_RETRIEVAL_MAX_LIMIT",
    "CRE_RETRIEVAL_STATEMENT_TIMEOUT_MS",
    "CRE_LLM_PROVIDER",
    "CRE_LLM_MODEL",
    "CRE_LLM_API_KEY",
    "CRE_LLM_TIMEOUT_SECONDS",
    "CRE_LLM_MAX_OUTPUT_TOKENS",
    "CRE_LLM_BUNDLE_MAX_ITEMS",
    "CRE_LLM_BUNDLE_MAX_CHARS",
    "CRE_LLM_BUNDLE_MAX_ITEM_CHARS",
    "CRE_INVESTIGATION_MAX_ITERATIONS",
    "CRE_INVESTIGATION_MAX_TOTAL_TOKENS",
    "CRE_INVESTIGATION_PER_QUERY_LIMIT",
    "CRE_INVESTIGATION_MAX_TOOL_CALLS_PER_ITERATION",
    "CRE_INVESTIGATION_WALL_CLOCK_SECONDS",
    "CRE_INVESTIGATION_LEASE_SECONDS",
    "CRE_INVESTIGATION_JOB_MAX_ATTEMPTS",
    "CRE_REPORT_DEMO",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_defaults() -> None:
    settings = get_settings()
    assert settings.database_url == ""
    assert settings.service_name == "cre-lab-api"
    assert settings.version == cre.__version__
    assert settings.commit == "unknown"
    assert settings.log_level == "INFO"
    assert settings.otel_exporter == "none"
    assert settings.worker_poll_seconds == 2.0


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRE_VERSION", "9.9.9-test")
    monkeypatch.setenv("CRE_COMMIT", "deadbeef")
    settings = Settings()
    assert settings.version == "9.9.9-test"
    assert settings.commit == "deadbeef"


def test_empty_env_values_fall_back_to_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRE_OTEL_EXPORTER", "")
    monkeypatch.setenv("CRE_VERSION", "")
    settings = Settings()
    assert settings.otel_exporter == "none"
    assert settings.version == cre.__version__


def test_invalid_exporter_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRE_OTEL_EXPORTER", "jaeger")
    with pytest.raises(ValidationError):
        Settings()


def test_poll_seconds_out_of_range_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRE_WORKER_POLL_SECONDS", "0")
    with pytest.raises(ValidationError):
        Settings()


def test_ingest_limit_defaults() -> None:
    settings = get_settings()
    assert settings.ingest_max_body_bytes == 1_048_576
    assert settings.ingest_max_items == 1000


def test_ingest_limit_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRE_INGEST_MAX_BODY_BYTES", "2048")
    monkeypatch.setenv("CRE_INGEST_MAX_ITEMS", "5")
    settings = Settings()
    assert settings.ingest_max_body_bytes == 2048
    assert settings.ingest_max_items == 5


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CRE_INGEST_MAX_BODY_BYTES", "1023"),
        ("CRE_INGEST_MAX_BODY_BYTES", "10485761"),
        ("CRE_INGEST_MAX_ITEMS", "0"),
        ("CRE_INGEST_MAX_ITEMS", "10001"),
    ],
)
def test_ingest_limits_out_of_range_raise(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings()


def test_retrieval_defaults() -> None:
    settings = get_settings()
    assert settings.retrieval_default_window_seconds == 900
    assert settings.retrieval_max_window_seconds == 86_400
    assert settings.retrieval_default_limit == 200
    assert settings.retrieval_max_limit == 1000
    assert settings.retrieval_statement_timeout_ms == 5000


def test_retrieval_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRE_RETRIEVAL_DEFAULT_WINDOW_SECONDS", "0")
    monkeypatch.setenv("CRE_RETRIEVAL_MAX_WINDOW_SECONDS", "60")
    monkeypatch.setenv("CRE_RETRIEVAL_DEFAULT_LIMIT", "5")
    monkeypatch.setenv("CRE_RETRIEVAL_MAX_LIMIT", "5")
    monkeypatch.setenv("CRE_RETRIEVAL_STATEMENT_TIMEOUT_MS", "100")
    monkeypatch.setenv("CRE_INVESTIGATION_PER_QUERY_LIMIT", "5")
    settings = Settings()
    assert settings.retrieval_default_window_seconds == 0
    assert settings.retrieval_max_window_seconds == 60
    assert settings.retrieval_default_limit == 5
    assert settings.retrieval_max_limit == 5
    assert settings.retrieval_statement_timeout_ms == 100


@pytest.mark.parametrize(
    "env",
    [
        {"CRE_RETRIEVAL_DEFAULT_WINDOW_SECONDS": "61", "CRE_RETRIEVAL_MAX_WINDOW_SECONDS": "60"},
        {"CRE_RETRIEVAL_DEFAULT_LIMIT": "11", "CRE_RETRIEVAL_MAX_LIMIT": "10"},
    ],
    ids=["window", "limit"],
)
def test_retrieval_default_above_max_raises(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CRE_RETRIEVAL_STATEMENT_TIMEOUT_MS", "50"),
        ("CRE_RETRIEVAL_STATEMENT_TIMEOUT_MS", "60001"),
        ("CRE_RETRIEVAL_DEFAULT_WINDOW_SECONDS", "-1"),
        ("CRE_RETRIEVAL_MAX_WINDOW_SECONDS", "0"),
        ("CRE_RETRIEVAL_MAX_WINDOW_SECONDS", "604801"),
        ("CRE_RETRIEVAL_DEFAULT_LIMIT", "0"),
        ("CRE_RETRIEVAL_MAX_LIMIT", "10001"),
    ],
)
def test_retrieval_settings_out_of_range_raise(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings()


def test_llm_defaults() -> None:
    settings = get_settings()
    assert settings.llm_provider == "fake"
    assert settings.llm_model == ""
    assert settings.llm_api_key.get_secret_value() == ""
    assert settings.llm_timeout_seconds == 30
    assert settings.llm_max_output_tokens == 4000
    assert settings.llm_bundle_max_items == 50
    assert settings.llm_bundle_max_chars == 48_000
    assert settings.llm_bundle_max_item_chars == 2000


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CRE_LLM_TIMEOUT_SECONDS", "0"),
        ("CRE_LLM_TIMEOUT_SECONDS", "301"),
        ("CRE_LLM_MAX_OUTPUT_TOKENS", "99"),
        ("CRE_LLM_BUNDLE_MAX_ITEMS", "0"),
        ("CRE_LLM_BUNDLE_MAX_CHARS", "10"),
        ("CRE_LLM_BUNDLE_MAX_ITEM_CHARS", "199"),
        ("CRE_LLM_PROVIDER", "openai"),
    ],
)
def test_llm_settings_invalid_raise(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings()


def test_llm_item_chars_above_max_chars_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRE_LLM_BUNDLE_MAX_CHARS", "1000")
    monkeypatch.setenv("CRE_LLM_BUNDLE_MAX_ITEM_CHARS", "1001")
    with pytest.raises(ValidationError):
        Settings()


def test_llm_api_key_is_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRE_LLM_API_KEY", "fake-key-value-123")
    monkeypatch.setenv("CRE_LLM_MODEL", "fake-model-x")
    settings = Settings()
    assert settings.llm_api_key.get_secret_value() == "fake-key-value-123"
    assert settings.llm_model == "fake-model-x"
    assert "fake-key-value-123" not in repr(settings)
    assert "fake-key-value-123" not in repr(settings.llm_api_key)


def test_investigation_defaults() -> None:
    settings = get_settings()
    assert settings.investigation_max_iterations == 3
    assert settings.investigation_max_total_tokens == 150_000
    assert settings.investigation_per_query_limit == 100
    assert settings.investigation_max_tool_calls_per_iteration == 3
    assert settings.investigation_wall_clock_seconds == 120


def test_investigation_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRE_INVESTIGATION_MAX_ITERATIONS", "10")
    monkeypatch.setenv("CRE_INVESTIGATION_MAX_TOTAL_TOKENS", "1000")
    monkeypatch.setenv("CRE_INVESTIGATION_PER_QUERY_LIMIT", "1000")
    monkeypatch.setenv("CRE_INVESTIGATION_MAX_TOOL_CALLS_PER_ITERATION", "1")
    monkeypatch.setenv("CRE_INVESTIGATION_WALL_CLOCK_SECONDS", "1.5")
    settings = Settings()
    assert settings.investigation_max_iterations == 10
    assert settings.investigation_max_total_tokens == 1000
    assert settings.investigation_per_query_limit == 1000
    assert settings.investigation_max_tool_calls_per_iteration == 1
    assert settings.investigation_wall_clock_seconds == 1.5


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CRE_INVESTIGATION_MAX_ITERATIONS", "0"),
        ("CRE_INVESTIGATION_MAX_ITERATIONS", "11"),
        ("CRE_INVESTIGATION_MAX_TOTAL_TOKENS", "999"),
        ("CRE_INVESTIGATION_MAX_TOTAL_TOKENS", "2000001"),
        ("CRE_INVESTIGATION_PER_QUERY_LIMIT", "0"),
        ("CRE_INVESTIGATION_PER_QUERY_LIMIT", "10001"),
        ("CRE_INVESTIGATION_MAX_TOOL_CALLS_PER_ITERATION", "0"),
        ("CRE_INVESTIGATION_MAX_TOOL_CALLS_PER_ITERATION", "11"),
        ("CRE_INVESTIGATION_WALL_CLOCK_SECONDS", "0.5"),
        ("CRE_INVESTIGATION_WALL_CLOCK_SECONDS", "3601"),
    ],
)
def test_investigation_settings_out_of_range_raise(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings()


def test_investigation_per_query_limit_above_retrieval_max_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRE_RETRIEVAL_DEFAULT_LIMIT", "10")
    monkeypatch.setenv("CRE_RETRIEVAL_MAX_LIMIT", "10")
    monkeypatch.setenv("CRE_INVESTIGATION_PER_QUERY_LIMIT", "11")
    with pytest.raises(ValidationError, match="CRE_INVESTIGATION_PER_QUERY_LIMIT"):
        Settings()


def test_investigation_job_defaults() -> None:
    settings = get_settings()
    assert settings.investigation_lease_seconds == 300
    assert settings.investigation_job_max_attempts == 3


def test_investigation_job_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRE_INVESTIGATION_LEASE_SECONDS", "5")
    monkeypatch.setenv("CRE_INVESTIGATION_JOB_MAX_ATTEMPTS", "20")
    settings = Settings()
    assert settings.investigation_lease_seconds == 5
    assert settings.investigation_job_max_attempts == 20


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CRE_INVESTIGATION_LEASE_SECONDS", "4.9"),
        ("CRE_INVESTIGATION_LEASE_SECONDS", "3601"),
        ("CRE_INVESTIGATION_JOB_MAX_ATTEMPTS", "0"),
        ("CRE_INVESTIGATION_JOB_MAX_ATTEMPTS", "21"),
    ],
)
def test_investigation_job_settings_out_of_range_raise(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings()


def test_report_demo_defaults_false() -> None:
    assert get_settings().report_demo is False


def test_report_demo_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRE_REPORT_DEMO", "true")
    assert Settings().report_demo is True
