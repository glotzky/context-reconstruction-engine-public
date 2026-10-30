import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from cre.config import Settings
from cre.investigator import investigate
from cre.llm import LLMProviderError, LLMRequest, LLMUsage
from cre.llm_anthropic import ANTHROPIC_MESSAGES_URL, ANTHROPIC_VERSION, AnthropicProvider
from tests.test_investigator import (
    ANCHOR,
    CONCLUDE,
    INITIAL,
    NEW,
    SCENARIO,
    SETTINGS,
    TRACE_CALL,
    FakeExecutor,
    _revision,
    _tools,
)

KEY = "sk-fake-test-key-000"
MODEL = "fake-anthropic-model"

Handler = Callable[[httpx.Request], httpx.Response]


def _ok(
    text: str = "fake answer", input_tokens: int = 10, output_tokens: int = 5
) -> dict[str, Any]:
    return {
        "id": "msg_fake",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


def _request(user: str = "fake user prompt", system: str = "fake system") -> LLMRequest:
    return LLMRequest(system=system, user=user, max_output_tokens=123, timeout_seconds=7.5)


class Recorder:
    def __init__(self, *responses: httpx.Response | Exception | dict[str, Any]) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, dict):
            return httpx.Response(200, json=item)
        return item


def _provider(
    handler: Handler,
    *,
    max_retries: int = 2,
    price_in: float = 0.0,
    price_out: float = 0.0,
) -> tuple[AnthropicProvider, list[float]]:
    sleeps: list[float] = []
    provider = AnthropicProvider(
        model=MODEL,
        api_key=SecretStr(KEY),
        max_retries=max_retries,
        price_input_per_mtok=price_in,
        price_output_per_mtok=price_out,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps.append,
    )
    return provider, sleeps


def test_success_returns_text_and_sends_expected_request() -> None:
    handler = Recorder(_ok("hello fake"))
    provider, sleeps = _provider(handler)
    request = _request()
    assert provider.complete(request) == "hello fake"
    (sent,) = handler.requests
    assert sent.method == "POST"
    assert str(sent.url) == ANTHROPIC_MESSAGES_URL
    assert sent.headers["x-api-key"] == KEY
    assert sent.headers["anthropic-version"] == ANTHROPIC_VERSION
    assert sent.headers["content-type"] == "application/json"
    assert json.loads(sent.content) == {
        "model": MODEL,
        "max_tokens": 123,
        "system": "fake system",
        "messages": [{"role": "user", "content": "fake user prompt"}],
    }
    timeout = sent.extensions["timeout"]
    assert set(timeout.values()) == {request.timeout_seconds}
    assert sleeps == []


def test_success_concatenates_multiple_text_blocks_and_ignores_non_text() -> None:
    body = _ok()
    body["content"] = [
        {"type": "text", "text": "part one, "},
        {"type": "tool_use", "id": "toolu_fake", "name": "x", "input": {}},
        {"type": "thinking", "thinking": "ignored"},
        {"type": "text", "text": "part two"},
    ]
    provider, _ = _provider(Recorder(body))
    assert provider.complete(_request()) == "part one, part two"


def test_max_tokens_stop_reason_still_returns_text() -> None:
    body = _ok('{"truncated": ')
    body["stop_reason"] = "max_tokens"
    provider, _ = _provider(Recorder(body))
    assert provider.complete(_request()) == '{"truncated": '
    assert len(provider.usage) == 1


def test_usage_and_cost_recorded_per_call() -> None:
    handler = Recorder(_ok("a", 1_000, 200), _ok("b", 2_000_000, 1_000_000))
    provider, _ = _provider(handler, price_in=3.0, price_out=15.0)
    provider.complete(_request())
    provider.complete(_request())
    first, second = provider.usage
    assert (first.input_tokens, first.output_tokens) == (1_000, 200)
    assert first.cost == pytest.approx(0.006)
    assert second == LLMUsage(input_tokens=2_000_000, output_tokens=1_000_000, cost=21.0)
    total = provider.total_usage
    assert (total.input_tokens, total.output_tokens) == (2_001_000, 1_000_200)
    assert total.cost == pytest.approx(21.006)


def test_zero_prices_give_zero_cost() -> None:
    provider, _ = _provider(Recorder(_ok("a", 500, 500)))
    provider.complete(_request())
    assert provider.usage == [LLMUsage(input_tokens=500, output_tokens=500, cost=0.0)]


def test_total_usage_empty_is_zero() -> None:
    provider, _ = _provider(Recorder(_ok()))
    assert provider.usage == []
    assert provider.total_usage == LLMUsage(input_tokens=0, output_tokens=0, cost=0.0)


@pytest.mark.parametrize("status", [400, 401, 404])
def test_4xx_not_retried(status: int) -> None:
    handler = Recorder(httpx.Response(status, text="fake error body"))
    provider, sleeps = _provider(handler)
    with pytest.raises(LLMProviderError) as info:
        provider.complete(_request())
    assert str(info.value) == f"anthropic request failed: http {status}"
    assert len(handler.requests) == 1
    assert sleeps == []
    assert provider.usage == []


def test_429_retried_then_succeeds() -> None:
    handler = Recorder(httpx.Response(429), _ok("after retry"))
    provider, sleeps = _provider(handler)
    assert provider.complete(_request()) == "after retry"
    assert len(handler.requests) == 2
    assert sleeps == [1.0]
    assert len(provider.usage) == 1


def test_429_exhausts_retries() -> None:
    handler = Recorder(httpx.Response(429))
    provider, sleeps = _provider(handler, max_retries=2)
    with pytest.raises(
        LLMProviderError, match="^anthropic rate limited: http 429 after 3 attempts$"
    ):
        provider.complete(_request())
    assert len(handler.requests) == 3
    assert len(sleeps) == 2
    assert provider.usage == []


@pytest.mark.parametrize("status", [500, 503, 529])
def test_5xx_exhausts_retries(status: int) -> None:
    handler = Recorder(httpx.Response(status))
    provider, sleeps = _provider(handler, max_retries=2)
    with pytest.raises(LLMProviderError) as info:
        provider.complete(_request())
    assert str(info.value) == f"anthropic server error: http {status} after 3 attempts"
    assert len(handler.requests) == 3
    assert sleeps == [1.0, 2.0]


def test_timeout_exhausts_retries() -> None:
    handler = Recorder(httpx.ReadTimeout("fake timeout"))
    provider, sleeps = _provider(handler, max_retries=3)
    with pytest.raises(LLMProviderError, match="^anthropic request timed out after 4 attempts$"):
        provider.complete(_request())
    assert len(handler.requests) == 4
    assert len(sleeps) == 3


def test_transport_error_retried() -> None:
    handler = Recorder(httpx.ConnectError("fake connect"), _ok("connected"))
    provider, sleeps = _provider(handler)
    assert provider.complete(_request()) == "connected"
    assert len(handler.requests) == 2
    assert sleeps == [1.0]


def test_transport_error_exhausts_retries() -> None:
    handler = Recorder(httpx.ConnectError("fake connect"))
    provider, _ = _provider(handler, max_retries=1)
    with pytest.raises(LLMProviderError, match="^anthropic transport error after 2 attempts$"):
        provider.complete(_request())


def test_zero_retries_single_attempt() -> None:
    handler = Recorder(httpx.Response(503))
    provider, sleeps = _provider(handler, max_retries=0)
    with pytest.raises(LLMProviderError, match="after 1 attempts$"):
        provider.complete(_request())
    assert len(handler.requests) == 1
    assert sleeps == []


def test_backoff_is_exponential_and_capped() -> None:
    handler = Recorder(httpx.Response(503))
    provider, sleeps = _provider(handler, max_retries=5)
    with pytest.raises(LLMProviderError):
        provider.complete(_request())
    assert len(handler.requests) == 6
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 8.0]


@pytest.mark.parametrize(
    ("status", "retry_after", "expected"),
    [
        (429, "3", 3.0),
        (503, "120", 8.0),
        (429, "abc", 1.0),
        (503, "-5", 1.0),
        (429, "nan", 1.0),
        (529, "0.5", 0.5),
    ],
)
def test_retry_after_honored_and_capped(status: int, retry_after: str, expected: float) -> None:
    handler = Recorder(httpx.Response(status, headers={"retry-after": retry_after}), _ok())
    provider, sleeps = _provider(handler)
    provider.complete(_request())
    assert sleeps == [expected]


def _with(**fields: Any) -> dict[str, Any]:
    body = _ok()
    body.update(fields)
    return body


def _without(name: str) -> dict[str, Any]:
    body = _ok()
    del body[name]
    return body


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not json at all"),
        httpx.Response(200, json={}),
        httpx.Response(200, json=_without("usage")),
        httpx.Response(200, json=_without("content")),
        httpx.Response(200, json=_with(usage={"input_tokens": -1, "output_tokens": 5})),
        httpx.Response(200, json=_with(content=[{"type": "tool_use", "id": "x", "input": {}}])),
        httpx.Response(200, json=_with(content=[{"type": "text", "text": 123}])),
        httpx.Response(200, json=["not", "an", "object"]),
    ],
    ids=[
        "non_json",
        "empty_object",
        "missing_usage",
        "missing_content",
        "negative_usage",
        "only_non_text",
        "text_not_string",
        "json_list",
    ],
)
def test_malformed_body_raises(response: httpx.Response) -> None:
    handler = Recorder(response)
    provider, sleeps = _provider(handler)
    with pytest.raises(LLMProviderError) as info:
        provider.complete(_request())
    assert str(info.value) == "anthropic response malformed"
    assert info.value.__cause__ is None
    assert len(handler.requests) == 1
    assert sleeps == []
    assert provider.usage == []


def test_key_and_prompt_never_in_logs_or_errors(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="httpx")
    caplog.set_level(logging.DEBUG, logger="httpcore")
    prompt_marker = "PROMPT-MARKER-7f3a"
    output_marker = "OUTPUT-MARKER-9c1d"
    request = _request(user=f"user {prompt_marker}", system=f"system {prompt_marker}")
    echo = f"{prompt_marker} {output_marker} {KEY}"

    provider, _ = _provider(Recorder(_ok(f"answer {output_marker}")))
    assert output_marker in provider.complete(request)
    failures = [
        Recorder(httpx.Response(401, text=echo)),
        Recorder(httpx.Response(503, text=echo)),
        Recorder(httpx.ReadTimeout(echo)),
        Recorder(httpx.Response(200, text=f"not json {echo}")),
    ]
    errors: list[LLMProviderError] = []
    providers = [provider]
    for handler in failures:
        failing, _ = _provider(handler)
        providers.append(failing)
        with pytest.raises(LLMProviderError) as info:
            failing.complete(request)
        errors.append(info.value)

    secrets = (KEY, prompt_marker, output_marker)
    for secret in secrets:
        assert secret not in caplog.text
        for exc in errors:
            assert secret not in str(exc)
            assert secret not in repr(exc)
        for item in providers:
            assert secret not in repr(item)
            assert secret not in str(item)
    for exc in errors:
        assert exc.__cause__ is None
        assert exc.__suppress_context__ is True


def test_close_only_closes_owned_client() -> None:
    injected, _ = _provider(Recorder(_ok()))
    injected.close()
    assert not injected._client.is_closed

    owned = AnthropicProvider(
        model=MODEL,
        api_key=SecretStr(KEY),
        max_retries=0,
        price_input_per_mtok=0.0,
        price_output_per_mtok=0.0,
    )
    assert not owned._client.is_closed
    owned.close()
    assert owned._client.is_closed


def test_from_settings_uses_settings_values() -> None:
    settings = Settings(
        llm_provider="anthropic",
        llm_model=MODEL,
        llm_api_key=SecretStr(KEY),
        llm_max_retries=4,
        llm_price_input_per_mtok=3.0,
        llm_price_output_per_mtok=15.0,
    )
    handler = Recorder(httpx.Response(503))
    sleeps: list[float] = []
    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = AnthropicProvider.from_settings(settings, client=client, sleep=sleeps.append)
    assert provider.name == "anthropic"
    assert provider.model == MODEL
    with pytest.raises(LLMProviderError, match="after 5 attempts$"):
        provider.complete(_request())
    assert len(handler.requests) == 5
    assert len(sleeps) == 4

    ok = Recorder(_ok("a", 1_000_000, 1_000_000))
    priced = AnthropicProvider.from_settings(
        settings, client=httpx.Client(transport=httpx.MockTransport(ok))
    )
    priced.complete(_request())
    assert ok.requests[0].headers["x-api-key"] == KEY
    assert priced.usage == [LLMUsage(input_tokens=1_000_000, output_tokens=1_000_000, cost=18.0)]


def test_investigator_completes_through_adapter() -> None:
    handler = Recorder(_ok(INITIAL), _ok(_tools(TRACE_CALL)), _ok(_revision()), _ok(CONCLUDE))
    provider, _ = _provider(handler)
    result = investigate(
        SCENARIO,
        anchor=ANCHOR,
        provider=provider,
        executor=FakeExecutor([NEW]),
        settings=SETTINGS,
    )
    assert result.stop_reason == "concluded"
    assert result.provider == "anthropic"
    assert result.model == MODEL
    assert len(handler.requests) == 4
    assert len(provider.usage) == 4


def test_last_stop_reason_is_remembered_per_call() -> None:
    truncated = _ok("{")
    truncated["stop_reason"] = "max_tokens"
    provider, _ = _provider(Recorder(truncated, _ok("done")))
    assert provider.last_stop_reason is None
    provider.complete(_request())
    assert provider.last_stop_reason == "max_tokens"
    provider.complete(_request())
    assert provider.last_stop_reason == "end_turn"
