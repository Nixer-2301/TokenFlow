from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


_ENV_REFERENCE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def _json_number(value: str) -> int | float | str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    return int(number) if number.is_integer() else number


def user_data_dir() -> Path:
    """Return TokenFlow's per-user data directory."""
    return Path.home() / ".tokenflow"


def normalize_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    for suffix in ("/chat/completions", "/models"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
    return value.rstrip("/")


def models_endpoint(base_url: str) -> str:
    return f"{normalize_base_url(base_url)}/models"


def resolve_api_key(value: str) -> str:
    match = _ENV_REFERENCE.match(value.strip())
    if match:
        return os.getenv(match.group(1), "")
    return value.strip()


@dataclass
class ModelProfile:
    id: str
    currency: str = "USD"
    input_per_million: str = "0"
    output_per_million: str = "0"
    multiplier: str = "1"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ModelProfile":
        cost = raw.get("cost", {})
        if not isinstance(cost, dict):
            cost = {}
        return cls(
            id=str(raw.get("id", raw.get("name", ""))).strip(),
            currency=str(raw.get("currency", cost.get("currency", "USD"))).strip()
            or "USD",
            input_per_million=str(
                raw.get("input_per_million", cost.get("input", "0"))
            ),
            output_per_million=str(
                raw.get("output_per_million", cost.get("output", "0"))
            ),
            multiplier=str(
                raw.get(
                    "multiplier",
                    cost.get("multiplier", cost.get("muiltipliers", "1")),
                )
            ),
        )


@dataclass
class ProviderProfile:
    name: str
    base_url: str
    api_key: str
    models: list[ModelProfile] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], name: str | None = None) -> "ProviderProfile":
        models_raw = raw.get("models", [])
        models = [ModelProfile.from_dict(item) for item in models_raw if isinstance(item, dict)]
        return cls(
            name=str(raw.get("name", name or "")).strip(),
            base_url=normalize_base_url(
                str(raw.get("base_url", raw.get("BaseUrl", "")))
            ),
            api_key=str(raw.get("api_key", raw.get("apiKey", ""))),
            models=[model for model in models if model.id],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ModelStore:
    providers: list[ProviderProfile] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ModelStore":
        providers_raw = raw.get("providers")
        if isinstance(providers_raw, list):
            providers = [
                ProviderProfile.from_dict(item)
                for item in providers_raw
                if isinstance(item, dict)
            ]
        else:
            # Also accept the compact provider-keyed format used by TokenFlow
            # users, for example {"provider-name": {"BaseUrl": "...", ...}}.
            providers = [
                ProviderProfile.from_dict(value, name=key)
                for key, value in raw.items()
                if key != "version" and isinstance(value, dict)
            ]
        return cls([provider for provider in providers if provider.name and provider.base_url])

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        used_keys: set[str] = set()
        for provider in self.providers:
            base_key = re.sub(r"[^a-z0-9]+", "_", provider.name.casefold()).strip("_")
            base_key = base_key or "provider"
            key = base_key
            suffix = 2
            while key in used_keys:
                key = f"{base_key}_{suffix}"
                suffix += 1
            used_keys.add(key)
            result[key] = {
                "BaseUrl": provider.base_url,
                "apiKey": provider.api_key,
                "api": "openai-completion",
                "models": [
                    {
                        "id": model.id,
                        "cost": {
                            "currency": model.currency,
                            "input": _json_number(model.input_per_million),
                            "output": _json_number(model.output_per_million),
                            "muiltipliers": _json_number(model.multiplier),
                        },
                    }
                    for model in provider.models
                ],
            }
        return result

    def find_provider(self, base_url: str) -> ProviderProfile | None:
        normalized = normalize_base_url(base_url)
        return next((item for item in self.providers if item.base_url == normalized), None)

    def add_provider(self, provider: ProviderProfile) -> None:
        existing = self.find_provider(provider.base_url)
        if existing is None:
            self.providers.append(provider)
        else:
            existing.name = provider.name
            existing.api_key = provider.api_key

    def add_model(self, provider: ProviderProfile, model: ModelProfile) -> None:
        stored_provider = self.find_provider(provider.base_url)
        if stored_provider is None:
            self.providers.append(provider)
            stored_provider = provider
        for existing in stored_provider.models:
            if existing.id == model.id:
                existing.currency = model.currency
                existing.input_per_million = model.input_per_million
                existing.output_per_million = model.output_per_million
                existing.multiplier = model.multiplier
                return
        stored_provider.models.append(model)


class ModelStoreFile:
    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or user_data_dir() / "models.json").expanduser().resolve()

    def load(self) -> ModelStore:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ModelStore()
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"无法读取模型库 {self.path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError("模型库根节点必须是对象")
        return ModelStore.from_dict(raw)

    def save(self, store: ModelStore) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(store.to_dict(), ensure_ascii=False, indent=2) + "\n"
        fd, temporary = tempfile.mkstemp(prefix="models-", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def parse_model_ids(payload: Any) -> list[str]:
    if isinstance(payload, dict):
        payload = payload.get("data", payload.get("models", []))
    if not isinstance(payload, list):
        return []
    result: list[str] = []
    for item in payload:
        value = item.get("id") if isinstance(item, dict) else item
        if isinstance(value, str) and value.strip() and value.strip() not in result:
            result.append(value.strip())
    return sorted(result, key=str.casefold)


async def fetch_model_ids(
    base_url: str,
    api_key: str,
    *,
    timeout: float = 20,
    http_client: httpx.AsyncClient | None = None,
) -> list[str]:
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=timeout)
    try:
        response = await client.get(
            models_endpoint(base_url),
            headers={"Authorization": f"Bearer {resolve_api_key(api_key)}"},
        )
        if not response.is_success:
            detail = response.text[:300] or f"HTTP {response.status_code}"
            raise ValueError(f"模型接口返回 HTTP {response.status_code}: {detail}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError("模型接口返回的不是有效 JSON") from exc
        model_ids = parse_model_ids(payload)
        if not model_ids:
            raise ValueError("模型接口没有返回可用的模型 ID")
        return model_ids
    except httpx.HTTPError as exc:
        raise ValueError(f"获取模型列表失败: {exc}") from exc
    finally:
        if owns_client:
            await client.aclose()


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()
