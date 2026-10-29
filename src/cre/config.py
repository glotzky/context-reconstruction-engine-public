"""Runtime settings read from environment variables."""

from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

import cre


class Settings(BaseSettings):
    model_config = SettingsConfigDict(populate_by_name=True, extra="ignore", env_ignore_empty=True)

    database_url: str = Field(default="", alias="DATABASE_URL")
    service_name: str = Field(default="cre-lab-api", alias="CRE_SERVICE_NAME")
    version: str = Field(default=cre.__version__, alias="CRE_VERSION")
    commit: str = Field(default="unknown", alias="CRE_COMMIT")
    log_level: str = Field(default="INFO", alias="CRE_LOG_LEVEL")
    otel_exporter: Literal["none", "console", "otlp"] = Field(
        default="none", alias="CRE_OTEL_EXPORTER"
    )
    worker_poll_seconds: float = Field(default=2.0, ge=0.1, le=60, alias="CRE_WORKER_POLL_SECONDS")
    ingest_max_body_bytes: int = Field(
        default=1_048_576, ge=1024, le=10_485_760, alias="CRE_INGEST_MAX_BODY_BYTES"
    )
    ingest_max_items: int = Field(default=1000, ge=1, le=10_000, alias="CRE_INGEST_MAX_ITEMS")
    retrieval_default_window_seconds: int = Field(
        default=900, ge=0, le=604_800, alias="CRE_RETRIEVAL_DEFAULT_WINDOW_SECONDS"
    )
    retrieval_max_window_seconds: int = Field(
        default=86_400, ge=1, le=604_800, alias="CRE_RETRIEVAL_MAX_WINDOW_SECONDS"
    )
    retrieval_default_limit: int = Field(
        default=200, ge=1, le=10_000, alias="CRE_RETRIEVAL_DEFAULT_LIMIT"
    )
    retrieval_max_limit: int = Field(default=1000, ge=1, le=10_000, alias="CRE_RETRIEVAL_MAX_LIMIT")
    retrieval_statement_timeout_ms: int = Field(
        default=5000, ge=100, le=60_000, alias="CRE_RETRIEVAL_STATEMENT_TIMEOUT_MS"
    )

    llm_provider: Literal["fake", "anthropic"] = Field(default="fake", alias="CRE_LLM_PROVIDER")
    llm_model: str = Field(default="", alias="CRE_LLM_MODEL")
    llm_api_key: SecretStr = Field(default=SecretStr(""), alias="CRE_LLM_API_KEY")
    llm_timeout_seconds: float = Field(default=30, ge=1, le=300, alias="CRE_LLM_TIMEOUT_SECONDS")
    llm_max_output_tokens: int = Field(
        default=4000, ge=100, le=32_000, alias="CRE_LLM_MAX_OUTPUT_TOKENS"
    )
    llm_max_retries: int = Field(default=2, ge=0, le=5, alias="CRE_LLM_MAX_RETRIES")
    llm_price_input_per_mtok: float = Field(
        default=0.0, ge=0, le=1000, alias="CRE_LLM_PRICE_INPUT_PER_MTOK"
    )
    llm_price_output_per_mtok: float = Field(
        default=0.0, ge=0, le=1000, alias="CRE_LLM_PRICE_OUTPUT_PER_MTOK"
    )
    llm_bundle_max_items: int = Field(default=50, ge=1, le=1000, alias="CRE_LLM_BUNDLE_MAX_ITEMS")
    llm_bundle_max_chars: int = Field(
        default=48_000, ge=1000, le=1_000_000, alias="CRE_LLM_BUNDLE_MAX_CHARS"
    )
    llm_bundle_max_item_chars: int = Field(
        default=2000, ge=200, le=100_000, alias="CRE_LLM_BUNDLE_MAX_ITEM_CHARS"
    )

    investigation_max_iterations: int = Field(
        default=3, ge=1, le=10, alias="CRE_INVESTIGATION_MAX_ITERATIONS"
    )
    investigation_max_total_tokens: int = Field(
        default=150_000, ge=1_000, le=2_000_000, alias="CRE_INVESTIGATION_MAX_TOTAL_TOKENS"
    )
    investigation_per_query_limit: int = Field(
        default=100, ge=1, le=10_000, alias="CRE_INVESTIGATION_PER_QUERY_LIMIT"
    )
    investigation_max_tool_calls_per_iteration: int = Field(
        default=3, ge=1, le=10, alias="CRE_INVESTIGATION_MAX_TOOL_CALLS_PER_ITERATION"
    )
    investigation_wall_clock_seconds: float = Field(
        default=120, ge=1, le=3600, alias="CRE_INVESTIGATION_WALL_CLOCK_SECONDS"
    )
    investigation_lease_seconds: float = Field(
        default=300, ge=5, le=3600, alias="CRE_INVESTIGATION_LEASE_SECONDS"
    )
    investigation_job_max_attempts: int = Field(
        default=3, ge=1, le=20, alias="CRE_INVESTIGATION_JOB_MAX_ATTEMPTS"
    )
    report_demo: bool = Field(default=False, alias="CRE_REPORT_DEMO")

    @model_validator(mode="after")
    def _check_retrieval_defaults(self) -> Self:
        if self.retrieval_default_window_seconds > self.retrieval_max_window_seconds:
            raise ValueError(
                "CRE_RETRIEVAL_DEFAULT_WINDOW_SECONDS must not exceed "
                "CRE_RETRIEVAL_MAX_WINDOW_SECONDS"
            )
        if self.retrieval_default_limit > self.retrieval_max_limit:
            raise ValueError("CRE_RETRIEVAL_DEFAULT_LIMIT must not exceed CRE_RETRIEVAL_MAX_LIMIT")
        return self

    @model_validator(mode="after")
    def _check_llm_bundle_limits(self) -> Self:
        if self.llm_bundle_max_item_chars > self.llm_bundle_max_chars:
            raise ValueError(
                "CRE_LLM_BUNDLE_MAX_ITEM_CHARS must not exceed CRE_LLM_BUNDLE_MAX_CHARS"
            )
        return self

    @model_validator(mode="after")
    def _check_investigation_limits(self) -> Self:
        if self.investigation_per_query_limit > self.retrieval_max_limit:
            raise ValueError(
                "CRE_INVESTIGATION_PER_QUERY_LIMIT must not exceed CRE_RETRIEVAL_MAX_LIMIT"
            )
        return self


def get_settings() -> Settings:
    return Settings()
