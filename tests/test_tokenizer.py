from __future__ import annotations

from tokenflow_cli.tokenizer import count_tokens
from tokenflow_cli.prompt_generator import build_prompt


def test_token_count_is_positive() -> None:
    assert count_tokens("A small prompt") > 0


def test_generated_prompt_meets_minimum() -> None:
    prompt, token_count = build_prompt(20000)
    assert prompt
    assert token_count >= 20000
