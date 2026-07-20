from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .stats import RequestRecord


class JsonlLogger:
    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.path = directory / f"run-{stamp}.jsonl"
        self._file = self.path.open("a", encoding="utf-8")

    def write(self, record: RequestRecord) -> None:
        payload = {
            "request_id": record.request_id,
            "ok": record.ok,
            "status_code": record.status_code,
            "prompt_tokens": record.prompt_tokens,
            "completion_tokens": record.completion_tokens,
            "total_tokens": record.total_tokens,
            "elapsed_seconds": round(record.elapsed_seconds, 3),
            "attempts": record.attempts,
            "error": record.error,
            "output_requirement_met": record.output_requirement_met,
            "first_text_seconds": record.first_text_seconds,
            "input_spend_microcents": record.input_spend_microcents,
            "output_spend_microcents": record.output_spend_microcents,
            "total_spend_microcents": record.total_spend_microcents,
        }
        self._file.write(json.dumps(payload, ensure_ascii=True) + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()
