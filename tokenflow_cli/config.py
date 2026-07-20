from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .errors import ConfigError
from .pricing import Pricing
from .models import ModelProfile, ProviderProfile, resolve_api_key


@dataclass(frozen=True)
class Settings:
    base_url: str
    model: str
    api_key: str
    prompt_file: Path
    tokenizer: str
    min_input_tokens: int
    concurrency: int
    max_requests: int
    max_tokens: int
    minimum_completion_tokens: int
    temperature: float
    request_timeout: float
    first_token_timeout: float
    max_retries: int
    backoff_base_seconds: float
    circuit_breaker_threshold: int
    global_idle_timeout: float
    pricing: Pricing
    extra_body: dict[str, Any]
    logs_dir: Path

    @property
    def endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"


def _positive_int(data: dict[str, Any], key: str, *, minimum: int = 1) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"{key} must be an integer >= {minimum}")
    return value


def _number(data: dict[str, Any], key: str, *, minimum: float = 0) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
        raise ConfigError(f"{key} must be a number >= {minimum}")
    return float(value)


def _decimal(value: Any, key: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ConfigError(f"{key} must be a number >= 0")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ConfigError(f"{key} must be a number >= 0") from exc
    if not result.is_finite() or result < 0:
        raise ConfigError(f"{key} must be a number >= 0")
    return result


def load_settings(config_path: str | Path) -> Settings:
    config_path = Path(config_path).resolve()
    if not config_path.is_file():
        raise ConfigError(f"Configuration file does not exist: {config_path}")

    load_dotenv(config_path.parent / ".env")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("Configuration root must be a mapping")

    # Connection fields remain readable for old project-local configs. New
    # configs intentionally leave them empty; the TUI supplies a provider.
    base_url = raw.get("base_url", "")
    model = raw.get("model", "")
    api_key_env = raw.get("api_key_env", "OPENAI_API_KEY")
    if not isinstance(base_url, str):
        raise ConfigError("base_url must be a string")
    if not isinstance(model, str):
        raise ConfigError("model must be a string")
    if not isinstance(api_key_env, str) or not api_key_env.strip():
        raise ConfigError("api_key_env must be a non-empty string")

    # Prefer the environment variable. Direct api_key is supported for local
    # setups, but should not be committed to a shared repository.
    direct_api_key = raw.get("api_key")
    api_key = os.getenv(api_key_env)
    if not api_key and isinstance(direct_api_key, str):
        api_key = direct_api_key.strip()

    # Older local configs sometimes put the actual key in api_key_env. Accept
    # that shape so an existing config starts working, while documenting the
    # safer api_key_env + .env form for future use.
    if not api_key and api_key_env.startswith("sk-"):
        api_key = api_key_env.strip()
    # A provider-backed config has no API key in YAML. It is resolved later.
    if api_key == "replace-me":
        api_key = ""
    if (base_url.strip() or model.strip()) and not api_key:
        raise ConfigError(
            f"Set {api_key_env} in .env/the environment or add api_key to config"
        )

    prompt_file = Path(raw.get("prompt_file", "prompt.txt"))
    if not prompt_file.is_absolute():
        prompt_file = config_path.parent / prompt_file
    logs_dir = Path(raw.get("logs_dir", "logs"))
    if not logs_dir.is_absolute():
        logs_dir = config_path.parent / logs_dir
    tokenizer = raw.get("tokenizer", "cl100k_base")
    if not isinstance(tokenizer, str) or not tokenizer:
        raise ConfigError("tokenizer must be a non-empty string")

    min_input_tokens = _positive_int(raw, "min_input_tokens", minimum=1)
    concurrency = _positive_int(raw, "concurrency", minimum=1)
    max_requests = _positive_int(raw, "max_requests", minimum=1)
    max_tokens = _positive_int(raw, "max_tokens", minimum=1)
    minimum_completion_tokens = _positive_int(
        raw, "minimum_completion_tokens", minimum=1
    )
    if minimum_completion_tokens > max_tokens:
        raise ConfigError("minimum_completion_tokens cannot exceed max_tokens")
    temperature = _number(raw, "temperature", minimum=0)
    if temperature > 2:
        raise ConfigError("temperature must be between 0 and 2")
    request_timeout = _number(raw, "request_timeout", minimum=1)
    first_token_timeout = _number(raw, "first_token_timeout", minimum=1)
    max_retries = raw.get("max_retries")
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
        raise ConfigError("max_retries must be an integer >= 0")
    backoff_base_seconds = _number(raw, "backoff_base_seconds", minimum=0)
    circuit_breaker_threshold = _positive_int(
        raw, "circuit_breaker_threshold", minimum=1
    )
    global_idle_timeout = _number(raw, "global_idle_timeout", minimum=0)
    pricing_raw = raw.get("pricing", {})
    if not isinstance(pricing_raw, dict):
        raise ConfigError("pricing must be a mapping")
    if "input_per_million" in pricing_raw:
        input_price = _decimal(
            pricing_raw["input_per_million"], "pricing.input_per_million"
        ) * 100
    else:
        input_price = _decimal(
            pricing_raw.get("input_per_million_cents", 0),
            "pricing.input_per_million_cents",
        )
    if "output_per_million" in pricing_raw:
        output_price = _decimal(
            pricing_raw["output_per_million"], "pricing.output_per_million"
        ) * 100
    else:
        output_price = _decimal(
            pricing_raw.get("output_per_million_cents", 0),
            "pricing.output_per_million_cents",
        )
    currency = pricing_raw.get("currency", "USD")
    if not isinstance(currency, str) or not currency.strip():
        raise ConfigError("pricing.currency must be a non-empty string")
    multiplier = _decimal(pricing_raw.get("multiplier", 1), "pricing.multiplier")
    extra_body = raw.get("extra_body", {})
    if not isinstance(extra_body, dict):
        raise ConfigError("extra_body must be a mapping")
    protected_fields = {"model", "messages", "stream", "stream_options"}
    protected = protected_fields.intersection(extra_body)
    if protected:
        raise ConfigError(
            "extra_body cannot override: " + ", ".join(sorted(protected))
        )

    return Settings(
        base_url=base_url.rstrip("/"),
        model=model.strip(),
        api_key=api_key,
        prompt_file=prompt_file,
        tokenizer=tokenizer,
        min_input_tokens=min_input_tokens,
        concurrency=concurrency,
        max_requests=max_requests,
        max_tokens=max_tokens,
        minimum_completion_tokens=minimum_completion_tokens,
        temperature=temperature,
        request_timeout=request_timeout,
        first_token_timeout=first_token_timeout,
        max_retries=max_retries,
        backoff_base_seconds=backoff_base_seconds,
        circuit_breaker_threshold=circuit_breaker_threshold,
        global_idle_timeout=global_idle_timeout,
        pricing=Pricing(
            currency=currency.strip(),
            input_per_million_cents=input_price,
            output_per_million_cents=output_price,
            multiplier=multiplier,
        ),
        extra_body=dict(extra_body),
        logs_dir=logs_dir,
    )


def load_prompt(settings: Settings) -> tuple[str, int]:
    if not settings.prompt_file.is_file():
        raise ConfigError(
            f"Prompt file does not exist: {settings.prompt_file}. "
            "Run generate_prompt.py first."
        )
    prompt = settings.prompt_file.read_text(encoding="utf-8")
    from .tokenizer import count_tokens

    token_count = count_tokens(prompt, settings.tokenizer)
    if token_count < settings.min_input_tokens:
        raise ConfigError(
            f"Prompt has {token_count} tokens; at least "
            f"{settings.min_input_tokens} are required"
        )
    return prompt, token_count


def settings_with_profile(
    settings: Settings, provider: ProviderProfile, model: ModelProfile
) -> Settings:
    """Bind a selected model-library profile to request settings."""
    from dataclasses import replace

    try:
        input_price = Decimal(model.input_per_million)
        output_price = Decimal(model.output_per_million)
        multiplier = Decimal(model.multiplier)
        if any(
            not value.is_finite() or value < 0
            for value in (input_price, output_price, multiplier)
        ):
            raise InvalidOperation
        pricing = Pricing(
            currency=model.currency,
            input_per_million_cents=input_price * 100,
            output_per_million_cents=output_price * 100,
            multiplier=multiplier,
        )
    except (InvalidOperation, ValueError) as exc:
        raise ConfigError(f"模型 {model.id} 的价格配置无效") from exc
    if not model.id.strip() or not provider.base_url.strip():
        raise ConfigError("供应商和模型资料不完整")
    return replace(
        settings,
        base_url=provider.base_url,
        model=model.id,
        api_key=resolve_api_key(provider.api_key),
        pricing=pricing,
    )
