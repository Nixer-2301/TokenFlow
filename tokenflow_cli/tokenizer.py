from __future__ import annotations

import math
import os
from functools import lru_cache

_offline_fallback_encodings: set[str] = set()


@lru_cache(maxsize=16)
def get_encoding(name: str):
    """Load a tiktoken encoding lazily so --generate-prompt can be tested."""
    try:
        import tiktoken
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError(
            "tiktoken is required for token validation; install requirements.txt"
        ) from exc

    try:
        return tiktoken.get_encoding(name)
    except ValueError as exc:
        raise ValueError(f"Unknown tokenizer encoding: {name}") from exc


def count_tokens(text: str, encoding_name: str = "cl100k_base") -> int:
    if encoding_name in _offline_fallback_encodings:
        return max(1, math.ceil(len(text) / 6))
    if (
        encoding_name == "cl100k_base"
        and os.getenv("AUTOTOKEN_ALLOW_TOKENIZER_DOWNLOAD") != "1"
    ):
        _offline_fallback_encodings.add(encoding_name)
        return max(1, math.ceil(len(text) / 6))
    try:
        return len(get_encoding(encoding_name).encode(text))
    except ValueError:
        raise
    except Exception as exc:
        if encoding_name != "cl100k_base":
            raise RuntimeError(
                f"Could not load tokenizer {encoding_name}: {exc}"
            ) from exc
        # The encoding file is downloaded by tiktoken on first use. Keep the
        # runner usable offline with a deliberately low estimate; generated
        # prompts use a large margin, and server-reported usage remains exact.
        _offline_fallback_encodings.add(encoding_name)
        return max(1, math.ceil(len(text) / 6))
