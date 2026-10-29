"""Anthropic Messages API adapter for the ``LLMProvider`` protocol, using ``httpx``.

Retries and sleeps are bounded. Error messages are fixed and never contain the key, prompt,
output or response body. Nothing is logged here.
"""

import math
import time
from collections.abc import Callable
from typing import Annotated, Final

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from cre.config import Settings
from cre.llm import LLMProviderError, LLMRequest, LLMUsage, compute_cost

ANTHROPIC_MESSAGES_URL: Final = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION: Final = "2023-06-01"
BACKOFF_BASE_SECONDS: Final = 1.0
MAX_BACKOFF_SECONDS: Final = 8.0

_MALFORMED: Final = "anthropic response malformed"


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _Usage(_Response):
    input_tokens: Annotated[int, Field(ge=0)]
    output_tokens: Annotated[int, Field(ge=0)]


class _ContentBlock(_Response):
    type: str
    text: str | None = None


class _MessageResponse(_Response):
    content: list[_ContentBlock]
    usage: _Usage
    stop_reason: str | None = None


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return min(MAX_BACKOFF_SECONDS, value)


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        *,
        model: str,
        api_key: SecretStr,
        max_retries: int,
        price_input_per_mtok: float,
        price_output_per_mtok: float,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = model
        self._api_key = api_key
        self._max_retries = max_retries
        self._price_input_per_mtok = price_input_per_mtok
        self._price_output_per_mtok = price_output_per_mtok
        self._owns_client = client is None
        self._client = client if client is not None else httpx.Client()
        self._sleep = sleep
        self.usage: list[LLMUsage] = []

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> "AnthropicProvider":
        return cls(
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            max_retries=settings.llm_max_retries,
            price_input_per_mtok=settings.llm_price_input_per_mtok,
            price_output_per_mtok=settings.llm_price_output_per_mtok,
            client=client,
            sleep=sleep,
        )

    @property
    def total_usage(self) -> LLMUsage:
        return LLMUsage(
            input_tokens=sum(u.input_tokens for u in self.usage),
            output_tokens=sum(u.output_tokens for u in self.usage),
            cost=sum(u.cost for u in self.usage),
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def complete(self, request: LLMRequest) -> str:
        attempts = 1 + self._max_retries
        failure = ""
        for attempt in range(attempts):
            retry_after: float | None = None
            try:
                response = self._post(request)
            except httpx.TimeoutException:
                failure = f"anthropic request timed out after {attempt + 1} attempts"
            except httpx.TransportError:
                failure = f"anthropic transport error after {attempt + 1} attempts"
            else:
                status = response.status_code
                if 200 <= status < 300:
                    return self._parse(response)
                if status == 429:
                    failure = f"anthropic rate limited: http 429 after {attempt + 1} attempts"
                elif 500 <= status < 600:
                    failure = f"anthropic server error: http {status} after {attempt + 1} attempts"
                else:
                    raise LLMProviderError(f"anthropic request failed: http {status}") from None
                retry_after = _retry_after(response)
            if attempt + 1 < attempts:
                backoff = min(MAX_BACKOFF_SECONDS, BACKOFF_BASE_SECONDS * 2**attempt)
                self._sleep(retry_after if retry_after is not None else backoff)
        raise LLMProviderError(failure) from None

    def _post(self, request: LLMRequest) -> httpx.Response:
        return self._client.post(
            ANTHROPIC_MESSAGES_URL,
            headers={
                "x-api-key": self._api_key.get_secret_value(),
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": request.max_output_tokens,
                "system": request.system,
                "messages": [{"role": "user", "content": request.user}],
            },
            timeout=request.timeout_seconds,
        )

    def _parse(self, response: httpx.Response) -> str:
        try:
            message = _MessageResponse.model_validate(response.json())
        except (ValueError, ValidationError):
            raise LLMProviderError(_MALFORMED) from None
        texts = [b.text for b in message.content if b.type == "text" and isinstance(b.text, str)]
        if not texts:
            raise LLMProviderError(_MALFORMED) from None
        usage = message.usage
        self.usage.append(
            LLMUsage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost=compute_cost(
                    usage.input_tokens,
                    usage.output_tokens,
                    self._price_input_per_mtok,
                    self._price_output_per_mtok,
                ),
            )
        )
        return "".join(texts)
