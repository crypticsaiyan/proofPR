"""The model returns data, and invalid data is retried once and then fails."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx
from pydantic import BaseModel

from proofpr.adapters.openrouter import API_ROOT, OpenRouterAdapter, extract_json
from proofpr.domain.errors import PermanentAppError
from proofpr.guard import Guard


class Answer(BaseModel):
    """A minimal schema for exercising validation."""

    verdict: str
    confidence: float


@pytest.fixture
def mock_api() -> Iterator[respx.MockRouter]:
    """Intercept the OpenRouter API."""
    with respx.mock(base_url=API_ROOT, assert_all_called=False) as router:
        yield router


def build(guard: Guard) -> OpenRouterAdapter:
    """Build a model client with a fast retry policy."""
    return OpenRouterAdapter(
        api_key="k",
        cheap_model="anthropic/claude-haiku-4.5",
        strong_model="anthropic/claude-opus-5",
        guard=guard,
        attempts=1,
        backoff_initial=0.001,
    )


def completion(content: str, *, prompt: int = 100, output: int = 20) -> httpx.Response:
    """Build a chat completion response."""
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": output},
        },
    )


class TestExtractJson:
    """Models fence their JSON however they like; the parser copes."""

    def test_plain_json(self) -> None:
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_fenced_json(self) -> None:
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_json_with_prose_around_it(self) -> None:
        assert extract_json('Sure, here it is: {"a": 1} hope that helps') == {"a": 1}

    def test_no_json_at_all_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="no JSON object"):
            extract_json("I cannot help with that")


class TestCompletion:
    """Output is validated, costed, and stamped with the prompt version."""

    async def test_a_valid_response_is_returned_with_its_usage(
        self, guard: Guard, mock_api: respx.MockRouter
    ) -> None:
        mock_api.post("/chat/completions").mock(
            return_value=completion('{"verdict": "bug", "confidence": 0.9}')
        )

        answer, usage = await build(guard).complete_json(
            system="s", user="u", schema=Answer, tier="cheap"
        )

        assert answer.verdict == "bug"
        assert usage.input_tokens == 100
        assert usage.cost_usd > 0
        assert usage.cost_known is True
        assert usage.prompt_version

    async def test_an_invalid_response_is_retried_once_with_the_error(
        self, guard: Guard, mock_api: respx.MockRouter
    ) -> None:
        route = mock_api.post("/chat/completions").mock(
            side_effect=[
                completion('{"verdict": "bug"}'),
                completion('{"verdict": "bug", "confidence": 0.5}'),
            ]
        )

        answer, usage = await build(guard).complete_json(system="s", user="u", schema=Answer)

        assert answer.confidence == 0.5
        assert usage.retried is True
        assert route.call_count == 2
        # The retry must tell the model what was wrong, or it repeats itself.
        assert "did not match the schema" in route.calls[1].request.content.decode()

    async def test_two_invalid_responses_fail_the_step(
        self, guard: Guard, mock_api: respx.MockRouter
    ) -> None:
        mock_api.post("/chat/completions").mock(return_value=completion("not json at all"))

        with pytest.raises(PermanentAppError, match="failed validation twice"):
            await build(guard).complete_json(system="s", user="u", schema=Answer)

    async def test_no_tools_are_ever_offered_to_the_model(
        self, guard: Guard, mock_api: respx.MockRouter
    ) -> None:
        route = mock_api.post("/chat/completions").mock(
            return_value=completion('{"verdict": "bug", "confidence": 0.9}')
        )

        await build(guard).complete_json(system="s", user="u", schema=Answer)

        body = route.calls[0].request.content.decode()
        assert "tools" not in body
        assert "function" not in body

    async def test_an_unknown_model_records_zero_cost_and_says_so(self, guard: Guard) -> None:
        adapter = build(guard)

        cost, known = adapter.price("someone/unlisted-model", 1000, 1000)

        assert cost == 0.0
        assert known is False

    async def test_a_response_without_choices_is_permanent(
        self, guard: Guard, mock_api: respx.MockRouter
    ) -> None:
        mock_api.post("/chat/completions").mock(
            return_value=httpx.Response(200, json={"error": "overloaded"})
        )

        with pytest.raises(PermanentAppError, match="unexpected response shape"):
            await build(guard).complete_json(system="s", user="u", schema=Answer)


def test_backticks_inside_a_json_string_do_not_truncate_it() -> None:
    # A patch for a file that itself handles fenced text contains ``` in its content.
    content = (
        '{"files": [{"path": "src/era.py", "content": "if t.startswith(\\"```\\"):\\n    pass"}]}'
    )
    assert extract_json(content)["files"][0]["path"] == "src/era.py"


def test_a_fence_wrapping_json_with_inner_backticks_still_unwraps() -> None:
    content = '```json\n{"content": "x = \\"```\\""}\n```'
    assert extract_json(content) == {"content": 'x = "```"'}
