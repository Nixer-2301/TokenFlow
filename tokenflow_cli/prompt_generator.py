from __future__ import annotations

import argparse
import os
from pathlib import Path

from .tokenizer import count_tokens

INTRO = """You are a principal software architect and technical author. Analyze the
software engineering dossier below and produce one self-contained answer.

This is a deliberate long-context exercise. Your answer MUST contain more than
10,000 completion tokens, meaning at least 10,001 completion tokens, when the
API permits it. Do not stop after a summary. Cover every numbered section, show
concrete code or pseudocode where useful, state assumptions, compare
alternatives, identify failure modes, and end with a practical implementation
and testing plan. Keep the answer coherent and avoid repeating paragraphs.
"""

TOPICS = [
    "domain boundaries and ownership", "API request and response contracts",
    "authentication and secret handling", "input normalization and Unicode",
    "tokenization and context-window accounting", "prompt versioning",
    "model selection and capability negotiation", "concurrency and queue fairness",
    "rate limiting and retry hints", "timeouts and cancellation",
    "idempotency and duplicate work", "retry classification and backoff",
    "circuit breaking and recovery", "partial concurrent failures",
    "graceful shutdown", "usage accounting and missing metadata",
    "cost estimation and billing reconciliation", "structured logging and privacy",
    "metrics and operator dashboards", "error taxonomy",
    "persistent checkpoints", "configuration validation", "load testing",
    "unit and contract tests", "mock servers and fault injection",
    "data retention", "prompt injection", "resource exhaustion",
    "deployment packaging", "rollout and rollback", "disaster recovery",
    "accessibility and developer ergonomics",
]


def build_section(number: int, topic: str) -> str:
    return f"""
## Evidence Section {number}: {topic}

The platform team is reviewing {topic} for a long-context processing service.
The service accepts a document, constructs a request for a selected language
model, sends it through a bounded asynchronous worker pool, and records the
result. Define normal behavior, at least three edge cases, observable signals,
operator response, authoritative state, and reconstructable state. Include one
concrete example and one counterexample. Explain how concurrency, incomplete
upstream metadata, interruption, reliability, security, cost, and
maintainability affect the decision. Compare a minimal implementation with a
production-oriented implementation and state when the extra complexity is
justified. Document assumptions precisely enough for implementation.
"""


def build_prompt(min_tokens: int = 20500, encoding: str = "cl100k_base") -> tuple[str, int]:
    target = max(20000, min_tokens)
    sections = [INTRO]
    number = 1
    while True:
        for topic in TOPICS:
            sections.append(build_section(number, topic))
            number += 1
            prompt = "\n".join(sections)
            measured = count_tokens(prompt, encoding)
            if measured >= target:
                return prompt, measured


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a TokenFlow long prompt")
    parser.add_argument("--output", type=Path, default=Path("prompt.txt"))
    parser.add_argument("--min-tokens", type=int, default=20500)
    parser.add_argument("--encoding", default="cl100k_base")
    args = parser.parse_args()
    if args.min_tokens < 20000:
        parser.error("--min-tokens must be at least 20000")
    prompt, token_count = build_prompt(args.min_tokens, args.encoding)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(prompt, encoding="utf-8")
    mode = (
        "estimated offline"
        if args.encoding == "cl100k_base"
        and os.getenv("AUTOTOKEN_ALLOW_TOKENIZER_DOWNLOAD") != "1"
        else "measured"
    )
    print(f"Wrote {args.output} with {token_count} {args.encoding} tokens ({mode})")


if __name__ == "__main__":
    main()
