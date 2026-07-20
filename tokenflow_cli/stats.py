from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from .pricing import Pricing


@dataclass(frozen=True)
class RequestRecord:
    request_id: int
    ok: bool
    status_code: int | None
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    elapsed_seconds: float
    attempts: int
    error: str | None = None
    output_requirement_met: bool | None = None
    first_text_seconds: float | None = None
    input_spend_microcents: int = 0
    output_spend_microcents: int = 0
    total_spend_microcents: int = 0


class Statistics:
    def __init__(
        self, minimum_completion_tokens: int, pricing: Pricing | None = None
    ) -> None:
        self.minimum_completion_tokens = minimum_completion_tokens
        self.pricing = pricing or Pricing()
        self.started = 0
        self.finished = 0
        self.successes = 0
        self.failures = 0
        self.retries = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.short_outputs = 0
        self.input_spend_microcents = 0
        self.output_spend_microcents = 0
        self.total_spend_microcents = 0
        self._lock = asyncio.Lock()
        self.started_at = time.monotonic()

    async def mark_started(self) -> None:
        async with self._lock:
            self.started += 1

    async def record(self, record: RequestRecord) -> None:
        async with self._lock:
            self.finished += 1
            self.retries += max(0, record.attempts - 1)
            if record.ok:
                self.successes += 1
            else:
                self.failures += 1
            self.prompt_tokens += record.prompt_tokens
            self.completion_tokens += record.completion_tokens
            self.total_tokens += record.total_tokens
            self.input_spend_microcents += record.input_spend_microcents
            self.output_spend_microcents += record.output_spend_microcents
            self.total_spend_microcents += record.total_spend_microcents
            if record.ok and record.completion_tokens < self.minimum_completion_tokens:
                self.short_outputs += 1

    async def snapshot(self) -> dict[str, int | float]:
        async with self._lock:
            elapsed = time.monotonic() - self.started_at
            return {
                "started": self.started,
                "finished": self.finished,
                "successes": self.successes,
                "failures": self.failures,
                "retries": self.retries,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens,
                "short_outputs": self.short_outputs,
                "spent_microcents": self.total_spend_microcents,
                "spent": self.total_spend_microcents / 100_000_000,
                "elapsed_seconds": round(elapsed, 3),
            }
