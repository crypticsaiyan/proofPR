"""OpenRouter model client.

The model returns JSON and nothing else. Every response is validated against a
pydantic schema before any caller sees it, and a response that does not validate
is retried once and then fails the step. There is no free-text path out of this
module, and no tool definitions are ever sent.

Cost is computed from the configured price table rather than guessed. An unknown
model yields a recorded zero with `cost_known=False`, so a missing price shows up
as a gap in the numbers instead of a quietly wrong total.
"""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from proofpr.adapters.base import FaultInjector, HttpAdapter
from proofpr.domain.errors import PermanentAppError
from proofpr.guard import Guard
from proofpr.observability.tracing import get_tracer, set_genai_attributes
from proofpr.prompts import version_hash

API_ROOT = "https://openrouter.ai/api/v1"

#: Dollars per million tokens, input and output. Overridden from configuration.
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "anthropic/claude-haiku-4.5": (1.0, 5.0),
    "anthropic/claude-opus-5": (5.0, 25.0),
    "anthropic/claude-sonnet-5": (2.0, 10.0),
}

#: Models sometimes wrap JSON in a fenced block despite being told not to.
WRAPPING_FENCE = re.compile(r"```(?:json)?\s*(.*)\s*```", re.DOTALL)

TSchema = TypeVar("TSchema", bound=BaseModel)


class ModelResponse(BaseModel):
    """One model call's usage record, written to the ledger."""

    model: str
    tier: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    cost_known: bool
    prompt_version: str
    retried: bool = False


class OpenRouterAdapter(HttpAdapter):
    """Model client with schema-validated output.

    It subclasses the HTTP adapter for retries and error mapping only. It
    performs no writes to any application, so it declares no write operations and
    never calls the guard.
    """

    app: ClassVar[str] = "openrouter"
    WRITE_OPERATIONS: ClassVar[frozenset[str]] = frozenset()

    def __init__(
        self,
        *,
        api_key: str,
        cheap_model: str,
        strong_model: str,
        guard: Guard,
        client: httpx.AsyncClient | None = None,
        faults: FaultInjector | None = None,
        prices: dict[str, tuple[float, float]] | None = None,
        attempts: int = 4,
        backoff_initial: float = 0.5,
    ) -> None:
        """Build the client.

        Args:
            api_key: OpenRouter key.
            cheap_model: Model for classification and duplicate verdicts.
            strong_model: Model for test synthesis and patches.
            guard: Present for interface symmetry; never used, since this
                adapter performs no writes.
            client: Optional preconfigured client.
            faults: Optional fault injector.
            prices: Dollars per million tokens, input and output, per model.
            attempts: Total attempts per call.
            backoff_initial: First backoff delay in seconds.
        """
        self.models = {"cheap": cheap_model, "strong": strong_model}
        self.prices = {**DEFAULT_PRICES, **(prices or {})}
        super().__init__(
            client=client
            or httpx.AsyncClient(
                base_url=API_ROOT,
                timeout=httpx.Timeout(connect=5.0, read=120.0, write=20.0, pool=5.0),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "X-Title": "ProofPR",
                },
            ),
            guard=guard,
            faults=faults,
            attempts=attempts,
            backoff_initial=backoff_initial,
        )

    def price(self, model: str, input_tokens: int, output_tokens: int) -> tuple[float, bool]:
        """Return the cost of a call and whether the price was known."""
        if model not in self.prices:
            return 0.0, False
        input_price, output_price = self.prices[model]
        cost = (input_tokens * input_price + output_tokens * output_price) / 1_000_000
        return round(cost, 6), True

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: type[TSchema],
        tier: str = "cheap",
        max_tokens: int = 2048,
    ) -> tuple[TSchema, ModelResponse]:
        """Return a schema-validated object and the call's usage record.

        One retry is allowed, and only for a response that fails validation. The
        retry says what was wrong, which is the difference between a model that
        corrects itself and one that repeats the same malformed answer.

        Raises:
            PermanentAppError: The response failed validation twice.
        """
        model = self.models[tier]
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        for attempt in (1, 2):
            with get_tracer().start_as_current_span("gen_ai.chat") as span:
                payload = await self._call(model, messages, max_tokens)
                # Reasoning models can return a null `content` (their output went to a
                # separate reasoning field, or the budget ran out). That is an invalid
                # answer to retry, not a crash: treated as empty, it fails validation.
                content = str(payload["choices"][0]["message"].get("content") or "")
                usage = payload.get("usage", {})
                input_tokens = int(usage.get("prompt_tokens", 0))
                output_tokens = int(usage.get("completion_tokens", 0))
                cost, known = self.price(model, input_tokens, output_tokens)
                set_genai_attributes(
                    span,
                    system="openrouter",
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost,
                )
            record = ModelResponse(
                model=model,
                tier=tier,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost,
                cost_known=known,
                prompt_version=version_hash(),
                retried=attempt == 2,
            )

            try:
                return schema.model_validate(extract_json(content)), record
            except (ValidationError, ValueError) as error:
                self._log.warning("model_output_invalid", attempt=attempt, error=str(error)[:300])
                if attempt == 2:
                    raise PermanentAppError(
                        f"model output failed validation twice: {error}", app=self.app
                    ) from error
                messages = [
                    *messages,
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": (
                            "That response did not match the schema. "
                            f"The error was: {error}. "
                            "Return only JSON matching the schema, with no prose and no code fence."
                        ),
                    },
                ]
        raise AssertionError("unreachable: the loop returns or raises")

    async def _call(
        self, model: str, messages: list[dict[str, str]], max_tokens: int
    ) -> dict[str, Any]:
        """Perform one chat completion call."""
        response = await self.request(
            "POST",
            "/chat/completions",
            operation="openrouter.complete",
            json={
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
        )
        payload: dict[str, Any] = response.json()
        if "choices" not in payload:
            raise PermanentAppError(f"unexpected response shape: {payload}", app=self.app)
        return payload


def extract_json(content: str) -> Any:  # noqa: ANN401 - the shape is the schema's business
    """Parse JSON out of a model response, fenced or not.

    The response is parsed as-is first. A fence is only unwrapped when it wraps
    the whole response: a patch's file contents can legitimately contain triple
    backticks, and cutting the JSON at the first one inside a string turns a valid
    answer into a fragment.

    Raises:
        ValueError: No JSON object could be found.
    """
    text = content.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    if wrapped := WRAPPING_FENCE.fullmatch(text):
        try:
            return json.loads(wrapped.group(1))
        except json.JSONDecodeError:
            text = wrapped.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError as error:
            raise ValueError(f"no JSON object in response: {error}") from error
    raise ValueError(f"no JSON object in response: {text[:200]!r}")
