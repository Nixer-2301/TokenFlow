from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from tokenflow_cli.models import (
    ModelProfile,
    ModelStore,
    ModelStoreFile,
    ProviderProfile,
    fetch_model_ids,
    models_endpoint,
    parse_model_ids,
    resolve_api_key,
)


def test_model_store_round_trips_provider_and_models(tmp_path: Path) -> None:
    store_file = ModelStoreFile(tmp_path / "models.json")
    store = ModelStore(
        [
            ProviderProfile(
                "Example",
                "https://example.test/v1",
                "sk-test",
                [ModelProfile("model-a", "CNY", "0.75", "4.5", "1.2")],
            )
        ]
    )
    store_file.save(store)

    loaded = store_file.load()
    assert loaded.providers[0].models[0].output_per_million == "4.5"
    assert store_file.path.read_text(encoding="utf-8").endswith("\n")


def test_model_store_reads_provider_keyed_schema(tmp_path: Path) -> None:
    path = tmp_path / "models.json"
    path.write_text(
        json.dumps(
            {
                "provider-name": {
                    "BaseUrl": "https://example.test/v1",
                    "apiKey": "sk-test",
                    "models": [
                        {
                            "name": "model-a",
                            "cost": {
                                "input": 0.75,
                                "output": 4.5,
                                "muiltipliers": 0.25,
                            },
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )

    loaded = ModelStoreFile(path).load()

    provider = loaded.providers[0]
    assert provider.name == "provider-name"
    assert provider.models[0].id == "model-a"
    assert provider.models[0].multiplier == "0.25"


def test_model_store_writes_provider_keyed_schema(tmp_path: Path) -> None:
    path = tmp_path / "models.json"
    ModelStoreFile(path).save(
        ModelStore(
            [
                ProviderProfile(
                    "provider-name",
                    "https://example.test/v1",
                    "sk-test",
                    [ModelProfile("model-a", "USD", "1", "2", "1")],
                )
            ]
        )
    )

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["provider_name"]["BaseUrl"] == "https://example.test/v1"
    assert raw["provider_name"]["models"][0]["id"] == "model-a"


def test_model_store_add_model_reuses_provider_by_base_url() -> None:
    stored = ProviderProfile("Example", "https://example.test/v1", "old-key")
    store = ModelStore([stored])
    incoming = ProviderProfile("Renamed", "https://example.test/v1", "new-key")

    store.add_provider(incoming)
    store.add_model(incoming, ModelProfile("model-a"))

    assert len(store.providers) == 1
    assert store.providers[0].api_key == "new-key"
    assert [model.id for model in store.providers[0].models] == ["model-a"]


def test_parse_model_ids_accepts_openai_data_and_deduplicates() -> None:
    assert parse_model_ids({"data": [{"id": "z"}, {"id": "a"}, {"id": "z"}]}) == [
        "a",
        "z",
    ]
    assert models_endpoint("https://example.test/v1/models/") == "https://example.test/v1/models"


def test_resolve_api_key_supports_environment_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKENFLOW_TEST_KEY", "secret")
    assert resolve_api_key("${TOKENFLOW_TEST_KEY}") == "secret"
    assert resolve_api_key("plain") == "plain"


@pytest.mark.asyncio
async def test_fetch_model_ids_uses_bearer_and_parses_response() -> None:
    observed: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["path"] = request.url.path
        observed["authorization"] = request.headers["authorization"]
        return httpx.Response(200, json={"data": [{"id": "model-b"}, {"id": "model-a"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fetch_model_ids(
            "https://example.test/v1",
            "sk-test",
            http_client=client,
        )

    assert result == ["model-a", "model-b"]
    assert observed == {
        "path": "/v1/models",
        "authorization": "Bearer sk-test",
    }


@pytest.mark.asyncio
async def test_fetch_model_ids_reports_empty_response() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="没有返回可用"):
            await fetch_model_ids("https://example.test/v1", "key", http_client=client)
