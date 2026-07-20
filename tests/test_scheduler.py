from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tokenflow_cli.client import ApiResult
from tokenflow_cli.request_log import JsonlLogger
from tokenflow_cli.scheduler import Scheduler
from tokenflow_cli.targets import RunTarget


class FakeClient:
    def __init__(self, results: list[ApiResult], delay: float = 0) -> None:
        self.results = iter(results)
        self.delay = delay
        self.max_tokens = 10001
        self.model = "test-model"
        self.active = 0
        self.max_active = 0

    async def request(
        self,
        _: str,
        on_first_text=None,
        model=None,
        on_text_progress=None,
        on_retry=None,
    ) -> ApiResult:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if on_first_text is not None:
                await on_first_text(0.01)
            if on_text_progress is not None:
                await on_text_progress(10001)
            return next(self.results)
        finally:
            self.active -= 1


def result(ok: bool) -> ApiResult:
    return ApiResult(
        ok=ok,
        status_code=200 if ok else 500,
        prompt_tokens=22000 if ok else 0,
        completion_tokens=10001 if ok else 0,
        total_tokens=32001 if ok else 0,
        elapsed_seconds=0.01,
        attempts=1,
        error=None if ok else "failure",
    )


@pytest.mark.asyncio
async def test_scheduler_honors_request_cap_and_concurrency(tmp_path: Path) -> None:
    client = FakeClient([result(True) for _ in range(5)], delay=0.01)
    logger = JsonlLogger(tmp_path)
    try:
        scheduler = Scheduler(
            client=client,
            prompt="prompt",
            max_requests=5,
            concurrency=2,
            circuit_breaker_threshold=5,
            minimum_completion_tokens=10001,
            logger=logger,
        )
        stats = await scheduler.run()
    finally:
        logger.close()

    snapshot = await stats.snapshot()
    assert client.max_active == 2
    assert snapshot["started"] == 5
    assert snapshot["successes"] == 5
    assert snapshot["total_tokens"] == 160005


@pytest.mark.asyncio
async def test_scheduler_opens_circuit_after_consecutive_failures(tmp_path: Path) -> None:
    client = FakeClient([result(False) for _ in range(10)])
    logger = JsonlLogger(tmp_path)
    try:
        scheduler = Scheduler(
            client=client,
            prompt="prompt",
            max_requests=10,
            concurrency=1,
            circuit_breaker_threshold=3,
            minimum_completion_tokens=10001,
            logger=logger,
        )
        stats = await scheduler.run()
    finally:
        logger.close()

    snapshot = await stats.snapshot()
    assert snapshot["started"] == 3
    assert snapshot["failures"] == 3


@pytest.mark.asyncio
async def test_scheduler_refills_slots_until_request_target(tmp_path: Path) -> None:
    events: list[tuple[str, int | None]] = []

    async def update(kind: str, request_id: int | None, _: dict) -> None:
        events.append((kind, request_id))

    client = FakeClient([result(True) for _ in range(7)], delay=0.005)
    logger = JsonlLogger(tmp_path)
    try:
        scheduler = Scheduler(
            client=client,
            prompt="prompt",
            target=RunTarget.requests(7),
            concurrency=3,
            circuit_breaker_threshold=3,
            minimum_completion_tokens=10001,
            logger=logger,
            on_update=update,
        )
        stats = await scheduler.run()
    finally:
        logger.close()

    snapshot = await stats.snapshot()
    assert snapshot["started"] == 7
    assert snapshot["finished"] == 7
    assert client.max_active == 3
    assert len([event for event in events if event[0] == "started"]) == 7


@pytest.mark.asyncio
async def test_scheduler_stops_dispatching_at_reserved_token_target(
    tmp_path: Path,
) -> None:
    client = FakeClient([result(True) for _ in range(10)])
    logger = JsonlLogger(tmp_path)
    try:
        scheduler = Scheduler(
            client=client,
            prompt="prompt",
            target=RunTarget.total_tokens(64_000),
            prompt_tokens=22_000,
            concurrency=10,
            circuit_breaker_threshold=3,
            minimum_completion_tokens=10001,
            logger=logger,
        )
        stats = await scheduler.run()
    finally:
        logger.close()

    snapshot = await stats.snapshot()
    assert snapshot["started"] == 2
    assert snapshot["finished"] == 2
