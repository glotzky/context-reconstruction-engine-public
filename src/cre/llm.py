"""LLM provider interface and the deterministic fake provider.

Core code depends only on the ``LLMProvider`` protocol, never on a vendor SDK. A real adapter
lives in its own module and imports its SDK lazily. Prompts and outputs are never logged.
"""

from collections.abc import Callable, Sequence
from typing import Annotated, Protocol

from pydantic import Field

from cre.config import Settings
from cre.evidence import _Model

FAKE_EMPTY_RESPONSE = '{"hypotheses": [], "unknowns": ["fake provider: no reasoning performed"]}'


class LLMProviderError(Exception):
    """The provider call failed. The message never contains the prompt or output."""


class LLMRequest(_Model):
    system: str
    user: str
    max_output_tokens: Annotated[int, Field(ge=1)]
    timeout_seconds: Annotated[float, Field(gt=0)]


class LLMUsage(_Model):
    input_tokens: Annotated[int, Field(ge=0)]
    output_tokens: Annotated[int, Field(ge=0)]
    cost: Annotated[float, Field(ge=0)]


def compute_cost(
    input_tokens: int,
    output_tokens: int,
    price_input_per_mtok: float,
    price_output_per_mtok: float,
) -> float:
    return (input_tokens * price_input_per_mtok + output_tokens * price_output_per_mtok) / 1_000_000


class LLMProvider(Protocol):
    name: str
    model: str

    def complete(self, request: LLMRequest) -> str: ...


class FakeProvider:
    """Deterministic provider for tests and local runs. Makes no network calls."""

    name = "fake"

    def __init__(
        self,
        responses: Sequence[str] | Callable[[LLMRequest], str],
        model: str = "fake-model",
    ) -> None:
        self.model = model
        self.requests: list[LLMRequest] = []
        self._callable: Callable[[LLMRequest], str] | None = None
        self._responses: list[str] = []
        if callable(responses):
            self._callable = responses
        else:
            self._responses = list(responses)
        self._next = 0

    def complete(self, request: LLMRequest) -> str:
        self.requests.append(request)
        if self._callable is not None:
            return self._callable(request)
        if self._next >= len(self._responses):
            raise LLMProviderError("fake provider has no more responses")
        response = self._responses[self._next]
        self._next += 1
        return response


def get_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "fake":
        return FakeProvider(
            lambda _request: FAKE_EMPTY_RESPONSE, model=settings.llm_model or "fake-model"
        )
    if settings.llm_provider == "anthropic":
        if not settings.llm_model:
            raise ValueError("CRE_LLM_MODEL is required for the anthropic provider")
        if not settings.llm_api_key.get_secret_value():
            raise ValueError("CRE_LLM_API_KEY is required for the anthropic provider")
        from cre.llm_anthropic import AnthropicProvider

        return AnthropicProvider.from_settings(settings)
    raise ValueError("unsupported LLM provider")
