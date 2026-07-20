from __future__ import annotations

import httpx
import pytest

from tokenflow_cli.client import OpenAICompatibleClient


@pytest.mark.asyncio
async def test_client_retries_429_and_parses_usage() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "answer"}}],
                "usage": {
                    "prompt_tokens": 22000,
                    "completion_tokens": 10001,
                    "total_tokens": 32001,
                },
            },
        )

    transport = httpx.MockTransport(handler)
    async with OpenAICompatibleClient(
        endpoint="https://example.test/v1/chat/completions",
        api_key="test-key",
        model="test-model",
        max_tokens=12000,
        temperature=0.7,
        timeout=5,
        max_retries=2,
        backoff_base_seconds=0,
        http_client=httpx.AsyncClient(transport=transport),
    ) as client:
        result = await client.request("long prompt")

    assert calls == 2
    assert result.ok is True
    assert result.attempts == 2
    assert result.total_tokens == 32001


@pytest.mark.asyncio
async def test_client_does_not_retry_non_retryable_4xx() -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    async with OpenAICompatibleClient(
        endpoint="https://example.test/v1/chat/completions",
        api_key="test-key",
        model="test-model",
        max_tokens=12000,
        temperature=0.7,
        timeout=5,
        max_retries=3,
        backoff_base_seconds=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    ) as client:
        result = await client.request("long prompt")

    assert calls == 1
    assert result.ok is False
    assert result.error == "bad request"


@pytest.mark.asyncio
async def test_client_reports_first_text_from_sse_stream() -> None:
    first_text: list[float] = []

    async def handler(_: httpx.Request) -> httpx.Response:
        body = (
            'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":" world"}}]}\n\n'
            'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":2,"total_tokens":4}}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body.encode(),
        )

    async with OpenAICompatibleClient(
        endpoint="https://example.test/v1/chat/completions",
        api_key="test-key",
        model="test-model",
        max_tokens=10001,
        temperature=0.7,
        timeout=5,
        first_token_timeout=1,
        max_retries=0,
        backoff_base_seconds=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    ) as client:
        result = await client.request("prompt", first_text.append)

    assert result.ok is True
    assert len(first_text) == 1
    assert result.first_text_seconds is not None
    assert result.total_tokens == 4


@pytest.mark.asyncio
async def test_client_retries_when_stream_has_no_text() -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            body = 'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
        else:
            body = (
                'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
                'data: {"choices":[],"usage":{"total_tokens":3}}\n\n'
                "data: [DONE]\n\n"
            )
        return httpx.Response(200, content=body.encode())

    async with OpenAICompatibleClient(
        endpoint="https://example.test/v1/chat/completions",
        api_key="test-key",
        model="test-model",
        max_tokens=10001,
        temperature=0.7,
        timeout=5,
        first_token_timeout=1,
        max_retries=1,
        backoff_base_seconds=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    ) as client:
        result = await client.request("prompt")

    assert calls == 2
    assert result.ok is True
    assert result.attempts == 2


@pytest.mark.asyncio
async def test_client_falls_back_when_stream_options_are_unsupported() -> None:
    payloads: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(request.read())
        if len(payloads) == 1:
            return httpx.Response(
                400,
                json={"error": {"message": "stream_options is unsupported"}},
            )
        body = (
            'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            'data: {"choices":[],"usage":{"total_tokens":3}}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, content=body.encode())

    async with OpenAICompatibleClient(
        endpoint="https://example.test/v1/chat/completions",
        api_key="test-key",
        model="test-model",
        max_tokens=10001,
        temperature=0.7,
        timeout=5,
        first_token_timeout=1,
        max_retries=0,
        backoff_base_seconds=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    ) as client:
        result = await client.request("prompt")

    assert result.ok is True
    assert len(payloads) == 2
    assert b"stream_options" in payloads[0]
    assert b"stream_options" not in payloads[1]


@pytest.mark.asyncio
async def test_client_reports_retry_status() -> None:
    calls = 0
    retries: list[tuple[int, str]] = []

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"error": {"message": "busy"}})
        return httpx.Response(
            200,
            content=(
                'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
                "data: [DONE]\n\n"
            ).encode(),
        )

    async with OpenAICompatibleClient(
        endpoint="https://example.test/v1/chat/completions",
        api_key="test-key",
        model="test-model",
        max_tokens=10001,
        temperature=0.7,
        timeout=5,
        first_token_timeout=1,
        max_retries=1,
        backoff_base_seconds=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    ) as client:
        result = await client.request(
            "prompt", on_retry=lambda attempt, error: retries.append((attempt, error))
        )

    assert result.ok is True
    assert retries == [(2, "busy")]
