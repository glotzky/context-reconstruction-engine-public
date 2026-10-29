from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

import cre
from cre.config import Settings
from cre.llm import (
    FAKE_EMPTY_RESPONSE,
    FakeProvider,
    LLMProviderError,
    LLMRequest,
    compute_cost,
    get_provider,
)
from cre.llm_anthropic import AnthropicProvider


def _request(user: str = "u") -> LLMRequest:
    return LLMRequest(system="s", user=user, max_output_tokens=10, timeout_seconds=1.0)


def test_fake_sequence_returns_in_order_and_records_requests() -> None:
    provider = FakeProvider(["one", "two"])
    first, second = _request("a"), _request("b")
    assert provider.complete(first) == "one"
    assert provider.complete(second) == "two"
    assert provider.requests == [first, second]
    assert provider.name == "fake"
    assert provider.model == "fake-model"


def test_fake_sequence_exhausted_raises() -> None:
    provider = FakeProvider(["only"])
    provider.complete(_request())
    with pytest.raises(LLMProviderError):
        provider.complete(_request())


def test_fake_callable_receives_request() -> None:
    seen: list[LLMRequest] = []

    def respond(request: LLMRequest) -> str:
        seen.append(request)
        return request.user.upper()

    provider = FakeProvider(respond, model="fake-other")
    request = _request("hello")
    assert provider.complete(request) == "HELLO"
    assert seen == [request]
    assert provider.model == "fake-other"


def test_get_provider_returns_fake_empty_response() -> None:
    provider = get_provider(Settings())
    assert provider.name == "fake"
    assert provider.complete(_request()) == FAKE_EMPTY_RESPONSE
    assert provider.complete(_request()) == FAKE_EMPTY_RESPONSE


FAKE_KEY = "sk-fake-test-key-000"


def test_get_provider_anthropic_returns_adapter() -> None:
    settings = Settings(
        llm_provider="anthropic", llm_model="fake-anthropic-model", llm_api_key=SecretStr(FAKE_KEY)
    )
    provider = get_provider(settings)
    assert isinstance(provider, AnthropicProvider)
    assert provider.name == "anthropic"
    assert provider.model == "fake-anthropic-model"
    provider.close()


def test_get_provider_anthropic_requires_model() -> None:
    settings = Settings(llm_provider="anthropic", llm_api_key=SecretStr(FAKE_KEY))
    with pytest.raises(ValueError, match="CRE_LLM_MODEL is required"):
        get_provider(settings)


def test_get_provider_anthropic_requires_key() -> None:
    settings = Settings(llm_provider="anthropic", llm_model="fake-anthropic-model")
    with pytest.raises(ValueError, match="CRE_LLM_API_KEY is required") as info:
        get_provider(settings)
    assert "sk-" not in str(info.value)
    assert FAKE_KEY not in str(info.value)


def test_compute_cost() -> None:
    assert compute_cost(1_000_000, 500_000, 3.0, 15.0) == pytest.approx(10.5)
    assert compute_cost(2_000, 1_000, 3.0, 15.0) == pytest.approx(0.021)
    assert compute_cost(0, 0, 3.0, 15.0) == 0.0


@pytest.mark.parametrize(
    "fields", [{"max_output_tokens": 0}, {"timeout_seconds": 0}], ids=["tokens", "timeout"]
)
def test_llm_request_rejects_zero_limits(fields: dict[str, float]) -> None:
    values: dict[str, object] = {
        "system": "s",
        "user": "u",
        "max_output_tokens": 10,
        "timeout_seconds": 1.0,
        **fields,
    }
    with pytest.raises(ValidationError):
        LLMRequest.model_validate(values)


BANNED_IMPORTS = ("openai", "anthropic", "google", "cohere", "mistralai", "litellm", "langchain")


@pytest.mark.parametrize("module", ["llm", "bundle", "hypotheses", "prompts", "llm_anthropic"])
def test_no_vendor_sdk_imports(module: str) -> None:
    source = (Path(cre.__file__).parent / f"{module}.py").read_text()
    for name in BANNED_IMPORTS:
        assert f"import {name}" not in source
        assert f"from {name}" not in source
