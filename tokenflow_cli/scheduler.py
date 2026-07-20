from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .client import ApiResult, OpenAICompatibleClient
from .pricing import Pricing
from .request_log import JsonlLogger
from .stats import RequestRecord, Statistics
from .targets import RunTarget, TargetController

ProgressCallback = Callable[[RequestRecord, dict[str, int | float]], Awaitable[None]]
UpdateCallback = Callable[[str, int | None, dict[str, object]], Awaitable[None]]


@dataclass(frozen=True)
class SchedulerEvent:
    kind: str
    request_id: int | None
    data: dict[str, object]


class Scheduler:
    """Keep the configured number of request slots full until a target is met."""

    def __init__(
        self,
        client: OpenAICompatibleClient,
        prompt: str,
        max_requests: int | None = None,
        concurrency: int = 1,
        circuit_breaker_threshold: int = 5,
        minimum_completion_tokens: int = 1,
        logger: JsonlLogger | None = None,
        on_progress: ProgressCallback | None = None,
        target: RunTarget | None = None,
        prompt_tokens: int = 0,
        pricing: Pricing | None = None,
        global_idle_timeout: float = 0,
        on_update: UpdateCallback | None = None,
        model: str | None = None,
    ) -> None:
        if target is None:
            target = RunTarget.requests(max_requests or 1)
        self.client = client
        self.prompt = prompt
        self.model = model
        self.target = target
        self.concurrency = max(1, concurrency)
        self.circuit_breaker_threshold = circuit_breaker_threshold
        self.prompt_tokens = prompt_tokens
        self.pricing = pricing or Pricing()
        self.global_idle_timeout = global_idle_timeout
        self.logger = logger
        self.on_progress = on_progress
        self.on_update = on_update
        self.stats = Statistics(minimum_completion_tokens, self.pricing)
        self.controller = TargetController(target)
        self._stop = asyncio.Event()
        self._failure_lock = asyncio.Lock()
        self._consecutive_failures = 0
        self._last_text_at = time.monotonic()
        self._tasks: dict[asyncio.Task[None], int] = {}
        self._waiting_for_text: set[int] = set()
        self._request_started_at: dict[int, float] = {}

    async def run(self) -> Statistics:
        await self._fill_slots()
        try:
            while self._tasks and not self._stop.is_set():
                timeout = self._idle_wait_timeout()
                done, _ = await asyncio.wait(
                    tuple(self._tasks),
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    # A request may have emitted its first text while wait()
                    # was sleeping. Re-check before treating it as global idle.
                    remaining = self._idle_wait_timeout()
                    if remaining is None or remaining > 0.02:
                        continue
                    self._stop.set()
                    await self._emit(
                        "idle_timeout",
                        None,
                        {"timeout": self.global_idle_timeout},
                    )
                    break
                for task in done:
                    self._tasks.pop(task, None)
                    try:
                        task.result()
                    except asyncio.CancelledError:
                        pass
                    except Exception as exc:
                        await self._emit("scheduler_error", None, {"error": str(exc)})
                await self._fill_slots()
        finally:
            await self._cancel_active()
        return self.stats

    async def stop(self) -> None:
        self._stop.set()
        await self._cancel_active()

    async def _fill_slots(self) -> None:
        while (
            not self._stop.is_set()
            and len(self._tasks) < self.concurrency
            and self.controller.can_start()
        ):
            request_id = self.controller.started + 1
            max_tokens = int(getattr(self.client, "max_tokens", 0))
            reserved = max(1, self.prompt_tokens) + max_tokens
            self.controller.reserve(self.prompt_tokens, max_tokens)
            await self.stats.mark_started()
            self._waiting_for_text.add(request_id)
            self._request_started_at[request_id] = time.monotonic()
            task = asyncio.create_task(self._run_one(request_id, reserved))
            self._tasks[task] = request_id
            await self._emit(
                "started",
                request_id,
                {
                    "model": self.model or getattr(self.client, "model", "default"),
                    "active": len(self._tasks),
                },
            )

    async def _run_one(self, request_id: int, reserved: int) -> None:
        await self._emit(
            "status",
            request_id,
            {
                "status": "starting",
                "model": self.model or getattr(self.client, "model", "default"),
            },
        )

        async def first_text(elapsed: float) -> None:
            self._last_text_at = time.monotonic()
            self._waiting_for_text.discard(request_id)
            await self._emit(
                "first_text",
                request_id,
                {"elapsed": elapsed, "status": "streaming"},
            )

        async def text_progress(completion_tokens: int) -> None:
            prompt_tokens = max(0, self.prompt_tokens)
            await self._emit(
                "progress",
                request_id,
                {
                    "status": "streaming",
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                    "spent_microcents": self.pricing.total_microcents(
                        prompt_tokens, completion_tokens
                    ),
                },
            )

        async def retry(next_attempt: int, error: str) -> None:
            await self._emit(
                "status",
                request_id,
                {
                    "status": "retrying",
                    "attempt": next_attempt,
                    "error": error,
                },
            )

        try:
            result = await self.client.request(
                self.prompt,
                on_first_text=first_text,
                model=self.model,
                on_text_progress=text_progress,
                on_retry=retry,
            )
        except asyncio.CancelledError:
            self._waiting_for_text.discard(request_id)
            await self._emit("status", request_id, {"status": "cancelled"})
            raise
        except Exception as exc:
            result = ApiResult(
                ok=False,
                status_code=None,
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                elapsed_seconds=max(
                    0.0,
                    time.monotonic()
                    - self._request_started_at.get(request_id, time.monotonic()),
                ),
                attempts=1,
                error=f"{type(exc).__name__}: {exc}",
            )

        prompt_tokens = result.prompt_tokens or (self.prompt_tokens if result.ok else 0)
        completion_tokens = result.completion_tokens or (
            result.estimated_completion_tokens if result.ok else 0
        )
        total_tokens = result.total_tokens or prompt_tokens + completion_tokens
        record = RequestRecord(
            request_id=request_id,
            ok=result.ok,
            status_code=result.status_code,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            elapsed_seconds=result.elapsed_seconds,
            attempts=result.attempts,
            error=result.error,
            output_requirement_met=(
                completion_tokens >= self.stats.minimum_completion_tokens
                if result.ok
                else None
            ),
            first_text_seconds=result.first_text_seconds,
            input_spend_microcents=self.pricing.input_microcents(prompt_tokens),
            output_spend_microcents=self.pricing.output_microcents(
                completion_tokens
            ),
            total_spend_microcents=self.pricing.total_microcents(
                prompt_tokens, completion_tokens
            ),
        )
        self._waiting_for_text.discard(request_id)
        self._request_started_at.pop(request_id, None)
        await self.stats.record(record)
        self.controller.complete(reserved, total_tokens)
        if self.logger is not None:
            self.logger.write(record)
        if self.on_progress is not None:
            await self.on_progress(record, await self.stats.snapshot())
        await self._update_failure_state(result.ok)
        await self._emit(
            "completed",
            request_id,
            {
                "status": "success" if result.ok else "failed",
                "record": record,
                "active": len(self._tasks) - 1,
                "snapshot": await self.stats.snapshot(),
            },
        )

    async def _update_failure_state(self, ok: bool) -> None:
        async with self._failure_lock:
            if ok:
                self._consecutive_failures = 0
                return
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.circuit_breaker_threshold:
                self._stop.set()
                await self._emit(
                    "circuit_breaker",
                    None,
                    {"threshold": self.circuit_breaker_threshold},
                )

    def _idle_wait_timeout(self) -> float | None:
        if (
            self.global_idle_timeout <= 0
            or not self._tasks
            or len(self._waiting_for_text) < len(self._tasks)
        ):
            return None
        newest_start = max(
            (self._request_started_at.get(request_id, self._last_text_at)
             for request_id in self._waiting_for_text),
            default=self._last_text_at,
        )
        idle_since = max(self._last_text_at, newest_start)
        return max(0.01, self.global_idle_timeout - (time.monotonic() - idle_since))

    async def _cancel_active(self) -> None:
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._waiting_for_text.clear()
        self._request_started_at.clear()

    async def _emit(
        self, kind: str, request_id: int | None, data: dict[str, object]
    ) -> None:
        if self.on_update is not None:
            await self.on_update(kind, request_id, data)
