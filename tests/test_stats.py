from __future__ import annotations

import pytest

from tokenflow_cli.stats import RequestRecord, Statistics


@pytest.mark.asyncio
async def test_stats_marks_short_successful_output() -> None:
    stats = Statistics(minimum_completion_tokens=10001)
    await stats.mark_started()
    await stats.record(
        RequestRecord(
            request_id=1,
            ok=True,
            status_code=200,
            prompt_tokens=22000,
            completion_tokens=10000,
            total_tokens=32000,
            elapsed_seconds=1,
            attempts=1,
            output_requirement_met=False,
        )
    )
    snapshot = await stats.snapshot()
    assert snapshot["short_outputs"] == 1
    assert snapshot["total_tokens"] == 32000
