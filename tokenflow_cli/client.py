from __future__ import annotations

import asyncio
import email.utils
import inspect
import json
import math
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

FirstTextCallback = Callable[[float], Awaitable[None] | None]
TextProgressCallback = Callable[[int], Awaitable[None] | None]
RetryCallback = Callable[[int, str], Awaitable[None] | None]


@dataclass(frozen=True)
class ApiResult:
    ok: bool
    status_code: int | None
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    elapsed_seconds: float
    attempts: int
    error: str | None = None
    first_text_seconds: float | None = None
    estimated_completion_tokens: int = 0


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            date = email.utils.parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


class OpenAICompatibleClient:
    def __init__(
        self,
        endpoint: str,
        api_key: str,
        model: str,
        max_tokens: int,
        temperature: float,
        timeout: float,
        max_retries: int,
        backoff_base_seconds: float,
        http_client: httpx.AsyncClient | None = None,
        first_token_timeout: float = 20,
        extra_body: dict[str, Any] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.first_token_timeout = first_token_timeout
        self.max_retries = max_retries
        self.backoff_base_seconds = backoff_base_seconds
        self._stream_options_enabled = True
        self.extra_body = dict(extra_body or {})
        self._owns_client = http_client is None
        self.http = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
        )

    async def __aenter__(self) -> "OpenAICompatibleClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client:
            await self.http.aclose()

    async def request(
        self,
        prompt: str,
        on_first_text: FirstTextCallback | None = None,
        model: str | None = None,
        on_text_progress: TextProgressCallback | None = None,
        on_retry: RetryCallback | None = None,
    ) -> ApiResult:
        started = time.monotonic()
        attempts = 0
        last_status: int | None = None
        last_error: str | None = None
        prompt_tokens = completion_tokens = total_tokens = 0
        first_text_seconds: float | None = None

        for attempt in range(self.max_retries + 1):
            attempts += 1
            try:
                attempt_started = time.monotonic()
                result = await self._request_once(
                    prompt,
                    attempt_started,
                    on_first_text,
                    include_stream_options=self._stream_options_enabled,
                    model=model,
                    on_text_progress=on_text_progress,
                )
                if self._stream_options_enabled and _is_stream_options_error(result):
                    self._stream_options_enabled = False
                    result = await self._request_once(
                        prompt,
                        attempt_started,
                        on_first_text,
                        include_stream_options=False,
                        model=model,
                        on_text_progress=on_text_progress,
                    )
                last_status = result["status_code"]
                prompt_tokens = result["prompt_tokens"]
                completion_tokens = result["completion_tokens"]
                total_tokens = result["total_tokens"]
                first_text_seconds = result["first_text_seconds"]
                if result["ok"]:
                    return ApiResult(
                        ok=True,
                        status_code=last_status,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        total_tokens=total_tokens,
                        elapsed_seconds=time.monotonic() - started,
                        attempts=attempts,
                        first_text_seconds=first_text_seconds,
                        estimated_completion_tokens=result[
                            "estimated_completion_tokens"
                        ],
                    )
                last_error = result["error"]
                if not result["retryable"]:
                    break
                if attempt < self.max_retries:
                    await _run_callback(on_retry, attempts + 1, str(last_error))
                    await self._sleep(
                        result["retry_after"]
                        if result["retry_after"] is not None
                        else self._backoff(attempt)
                    )
                continue
            except asyncio.TimeoutError:
                last_error = (
                    f"No text received within {self.first_token_timeout:g} seconds"
                )
                if attempt < self.max_retries:
                    await _run_callback(on_retry, attempts + 1, last_error)
                    await self._sleep(self._backoff(attempt))
                continue
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = f"{type(exc).__name__}: {exc}".strip()
                if attempt < self.max_retries:
                    await _run_callback(on_retry, attempts + 1, last_error)
                    await self._sleep(self._backoff(attempt))
                continue
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}".strip()
                break

            if attempt < self.max_retries:
                await self._sleep(self._backoff(attempt))

        return ApiResult(
            ok=False,
            status_code=last_status,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            elapsed_seconds=time.monotonic() - started,
            attempts=attempts,
            error=last_error or "request failed",
            first_text_seconds=first_text_seconds,
            estimated_completion_tokens=0,
        )

    async def _request_once(
        self,
        prompt: str,
        started: float,
        on_first_text: FirstTextCallback | None,
        include_stream_options: bool,
        model: str | None,
        on_text_progress: TextProgressCallback | None,
    ) -> dict[str, Any]:
        prompt_tokens = completion_tokens = total_tokens = 0
        first_text_seconds: float | None = None
        has_text = False
        text_characters = 0
        last_progress_tokens = 0
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": True,
        }
        payload.update(self.extra_body)
        if include_stream_options:
            payload["stream_options"] = {"include_usage": True}
        # This timeout covers connection setup, response headers, empty SSE
        # heartbeats, and role-only chunks. It is disabled after actual text.
        async with asyncio.timeout(self.first_token_timeout) as first_text_timer:
            async with self.http.stream("POST", self.endpoint, json=payload) as response:
                result = await self._consume_response(
                    response,
                    started,
                    on_first_text,
                    on_text_progress,
                    first_text_timer,
                )
        return result

    async def _consume_response(
        self,
        response: httpx.Response,
        started: float,
        on_first_text: FirstTextCallback | None,
        on_text_progress: TextProgressCallback | None,
        first_text_timer: asyncio.Timeout,
    ) -> dict[str, Any]:
        prompt_tokens = completion_tokens = total_tokens = 0
        first_text_seconds: float | None = None
        has_text = False
        text_characters = 0
        last_progress_tokens = 0
        if not response.is_success:
            await response.aread()
            return {
                "ok": False,
                "status_code": response.status_code,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "first_text_seconds": None,
                "error": _response_error(response),
                "retryable": _retryable_status(response.status_code),
                "retry_after": _retry_after(response.headers.get("retry-after")),
                "estimated_completion_tokens": 0,
            }

        lines = response.aiter_lines()
        while True:
            if not response.is_success:
                break
            try:
                line = await lines.__anext__()
            except StopAsyncIteration:
                break

            data = _event_data(line)
            if data is None:
                continue
            if data == "[DONE]":
                break
            try:
                body: dict[str, Any] = json.loads(data)
            except json.JSONDecodeError:
                continue

            usage = body.get("usage") or {}
            prompt_tokens = max(prompt_tokens, _usage_int(usage, "prompt_tokens"))
            completion_tokens = max(
                completion_tokens, _usage_int(usage, "completion_tokens")
            )
            total_tokens = max(total_tokens, _usage_int(usage, "total_tokens"))
            text = _chunk_text(body)
            if text:
                text_characters += len(text)
                estimated_tokens = max(1, math.ceil(text_characters / 4))
                if (
                    on_text_progress is not None
                    and estimated_tokens - last_progress_tokens >= 32
                ):
                    callback_result = on_text_progress(estimated_tokens)
                    if inspect.isawaitable(callback_result):
                        await callback_result
                    last_progress_tokens = estimated_tokens
            if text and not has_text:
                has_text = True
                first_text_timer.reschedule(None)
                first_text_seconds = time.monotonic() - started
                if on_first_text is not None:
                    callback_result = on_first_text(first_text_seconds)
                    if inspect.isawaitable(callback_result):
                        await callback_result

        if not has_text:
            return {
                "ok": False,
                "status_code": response.status_code,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "first_text_seconds": None,
                "error": "Stream completed without any text content",
                "retryable": True,
                "retry_after": None,
                "estimated_completion_tokens": 0,
            }

        estimated_completion_tokens = max(1, math.ceil(text_characters / 4))
        if (
            on_text_progress is not None
            and estimated_completion_tokens != last_progress_tokens
        ):
            callback_result = on_text_progress(estimated_completion_tokens)
            if inspect.isawaitable(callback_result):
                await callback_result
        if not total_tokens:
            completion_tokens = completion_tokens or estimated_completion_tokens
            total_tokens = prompt_tokens + completion_tokens
        return {
            "ok": True,
            "status_code": response.status_code,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "first_text_seconds": first_text_seconds,
            "error": None,
            "retryable": False,
            "retry_after": None,
            "estimated_completion_tokens": estimated_completion_tokens,
        }

    async def _sleep(self, seconds: float) -> None:
        if seconds > 0:
            await asyncio.sleep(seconds)

    def _backoff(self, attempt: int) -> float:
        return self.backoff_base_seconds * (2**attempt) + random.uniform(0, 0.25)


def _usage_int(usage: Any, key: str) -> int:
    value = usage.get(key, 0) if isinstance(usage, dict) else 0
    return value if isinstance(value, int) and value >= 0 else 0


def _retryable_status(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code <= 599


def _is_stream_options_error(result: dict[str, Any]) -> bool:
    if result.get("status_code") != 400:
        return False
    error = str(result.get("error") or "").lower()
    return "stream_options" in error or "include_usage" in error


def _response_error(response: httpx.Response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            message = body["error"].get("message")
            if message:
                return str(message)
    except ValueError:
        pass
    return response.text[:500] or f"HTTP {response.status_code}"


def _event_data(line: str) -> str | None:
    if line.startswith("data:"):
        return line[5:].strip()
    # Some compatible gateways ignore stream=true and return one JSON body.
    stripped = line.strip()
    return stripped if stripped.startswith("{") else None


def _chunk_text(body: dict[str, Any]) -> str:
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    choice = choices[0]
    if not isinstance(choice, dict):
        return ""
    delta = choice.get("delta")
    message = choice.get("message")
    content = (
        delta.get("content")
        if isinstance(delta, dict)
        else message.get("content")
        if isinstance(message, dict)
        else None
    )
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    return ""


async def _run_callback(callback: Callable[..., Any] | None, *args: object) -> None:
    if callback is None:
        return
    result = callback(*args)
    if inspect.isawaitable(result):
        await result
