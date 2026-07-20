from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RunTarget:
    mode: str
    value: int | None = None

    @classmethod
    def requests(cls, count: int) -> "RunTarget":
        if count < 1:
            raise ValueError("request target must be at least 1")
        return cls("requests", count)

    @classmethod
    def total_tokens(cls, count: int) -> "RunTarget":
        if count < 1:
            raise ValueError("token target must be at least 1")
        return cls("total_tokens", count)

    @classmethod
    def unlimited(cls) -> "RunTarget":
        return cls("unlimited")

    def label(self) -> str:
        if self.mode == "requests":
            return f"requests {self.value:,}"
        if self.mode == "total_tokens":
            return f"tokens {self.value:,}"
        return "unlimited"


class TargetController:
    def __init__(self, target: RunTarget) -> None:
        self.target = target
        self.started = 0
        self.confirmed_tokens = 0
        self.reserved_tokens = 0

    def can_start(self) -> bool:
        if self.target.mode == "unlimited":
            return True
        if self.target.mode == "requests":
            return self.started < (self.target.value or 0)
        if self.started == 0:
            return True
        return self.confirmed_tokens + self.reserved_tokens < (self.target.value or 0)

    def reserve(self, prompt_tokens: int, max_tokens: int) -> None:
        self.started += 1
        if self.target.mode == "total_tokens":
            self.reserved_tokens += max(1, prompt_tokens) + max_tokens

    def complete(self, reserved: int, total_tokens: int) -> None:
        if self.target.mode == "total_tokens":
            self.reserved_tokens = max(0, self.reserved_tokens - reserved)
            self.confirmed_tokens += max(0, total_tokens)

    def reached(self) -> bool:
        if self.target.mode == "requests":
            return self.started >= (self.target.value or 0)
        if self.target.mode == "total_tokens":
            return self.confirmed_tokens >= (self.target.value or 0)
        return False
