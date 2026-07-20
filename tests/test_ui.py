from __future__ import annotations

from pathlib import Path

import pytest

from tokenflow_cli.config import Settings
from tokenflow_cli.models import ModelProfile, ModelStore, ModelStoreFile, ProviderProfile
from tokenflow_cli.onboarding import ClipboardInput, ModelSetupApp
from tokenflow_cli.pricing import Pricing
from tokenflow_cli.targets import RunTarget
from tokenflow_cli.ui import (
    TokenFlowApp,
    LaunchSelection,
    RequestCard,
    compact_number,
    mask_api_key,
)


def settings(tmp_path: Path) -> Settings:
    return Settings(
        base_url="https://example.test/v1",
        model="test-model",
        api_key="sk-abcdefghijklmno",
        prompt_file=tmp_path / "prompt.txt",
        tokenizer="cl100k_base",
        min_input_tokens=1,
        concurrency=10,
        max_requests=10,
        max_tokens=10001,
        minimum_completion_tokens=10000,
        temperature=0.7,
        request_timeout=600,
        first_token_timeout=20,
        max_retries=1,
        backoff_base_seconds=1,
        circuit_breaker_threshold=2,
        global_idle_timeout=120,
        pricing=Pricing("USD", 300, 1500),
        extra_body={},
        logs_dir=tmp_path / "logs",
    )


def test_ui_helpers() -> None:
    assert mask_api_key("sk-abcdefghijklmno") == "sk-ab***mno"
    assert compact_number(12_345) == "12.3k"


def test_setup_accepts_empty_prices_and_has_terminal_paste_bindings() -> None:
    assert ModelSetupApp._valid_decimal("", default="0") == "0"
    assert ModelSetupApp._valid_decimal("", default="1") == "1"
    binding_keys = {
        binding.key if hasattr(binding, "key") else binding[0]
        for binding in ClipboardInput.BINDINGS
    }
    assert {"ctrl+v", "ctrl+shift+v", "shift+insert"} <= binding_keys
    assert ModelSetupApp._valid_base_url("https://provider.example/v1")
    assert not ModelSetupApp._valid_base_url("https://")


@pytest.mark.asyncio
async def test_setup_can_skip_all_prices(tmp_path: Path) -> None:
    app = ModelSetupApp(
        settings=settings(tmp_path),
        store_file=ModelStoreFile(tmp_path / "models.json"),
    )
    async with app.run_test(size=(100, 30)) as pilot:
        input_widget = app.query_one("#input", ClipboardInput)
        input_widget.value = "provider-name"
        await pilot.press("enter")
        input_widget.value = "https://provider.example/v1"
        await pilot.press("enter")
        input_widget.value = "api-key"
        await pilot.press("enter")
        await pilot.press("down", "enter")
        input_widget.value = "model-id"
        await pilot.press("enter")
        await pilot.press("enter")
        await pilot.press("enter")
        await pilot.press("enter")
        await pilot.press("enter")
        await pilot.pause()

        assert app.result is not None
        assert app.result.model.currency == "USD"
        assert app.result.model.input_per_million == "0"
        assert app.result.model.output_per_million == "0"
        assert app.result.model.multiplier == "1"


@pytest.mark.asyncio
async def test_tui_mounts_setup_and_sidebar(tmp_path: Path) -> None:
    app = TokenFlowApp(
        settings(tmp_path),
        prompt="prompt",
        prompt_tokens=20000,
        state_path=tmp_path / ".tokenflow" / "state.json",
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.query_one("#setup").display is True
        assert "TokenFlow" in str(app.query_one("#brand").render())
        assert "sk-ab***mno" in str(app.query_one("#connection").render())


@pytest.mark.asyncio
async def test_model_setup_lists_saved_profiles(tmp_path: Path) -> None:
    store = ModelStore(
        [
            ProviderProfile(
                "Example",
                "https://example.test/v1",
                "sk-test",
                [ModelProfile("model-a")],
            )
        ]
    )
    app = ModelSetupApp(
        settings=settings(tmp_path),
        store_file=ModelStoreFile(tmp_path / "models.json"),
        store=store,
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert app.stage == "catalog"
        await pilot.press("enter")
        await pilot.pause()
        assert app.result is not None
        assert app.result.provider.name == "Example"
        assert app.result.model.id == "model-a"


@pytest.mark.asyncio
async def test_tui_custom_request_wizard(tmp_path: Path) -> None:
    app = TokenFlowApp(
        settings(tmp_path),
        prompt="prompt",
        prompt_tokens=20000,
        state_path=tmp_path / ".tokenflow" / "state.json",
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("down", "enter")
        assert app.stage == "mode"
        await pilot.press("enter")
        assert app.stage == "target_value"
        await pilot.press("1", "2", "3", "enter")
        assert app.pending_target is not None
        assert app.pending_target.value == 123
        assert app.stage == "model_source"
        await pilot.press("enter")
        assert app.stage == "confirm"


@pytest.mark.asyncio
async def test_tui_grid_adapts_to_terminal_width(tmp_path: Path) -> None:
    app = TokenFlowApp(
        settings(tmp_path),
        prompt="prompt",
        prompt_tokens=20000,
        state_path=tmp_path / ".tokenflow" / "state.json",
    )
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        assert app.query_one("#request-grid").styles.grid_size_columns == 4


@pytest.mark.asyncio
async def test_completion_menu_can_return_to_settings(tmp_path: Path) -> None:
    app = TokenFlowApp(
        settings(tmp_path),
        prompt="prompt",
        prompt_tokens=20000,
        state_path=tmp_path / ".tokenflow" / "state.json",
    )
    async with app.run_test(size=(120, 40)) as pilot:
        app.selection = LaunchSelection(RunTarget.requests(10), "test-model")
        app.last_snapshot = {
            "total_tokens": 320_000,
            "successes": 10,
            "failures": 0,
        }
        app.run_stop_reason = "已达到请求次数目标"
        app._show_completion()
        await pilot.pause()
        assert app.stage == "complete"
        assert "320,000 Tokens" in str(
            app.query_one("#question-description").render()
        )
        await pilot.press("down", "enter")
        assert app.stage == "target_source"


@pytest.mark.asyncio
async def test_reset_run_view_clears_previous_cards(tmp_path: Path) -> None:
    app = TokenFlowApp(
        settings(tmp_path),
        prompt="prompt",
        prompt_tokens=20000,
        state_path=tmp_path / ".tokenflow" / "state.json",
    )
    async with app.run_test(size=(120, 40)) as pilot:
        card = RequestCard(1, "test-model")
        app.cards[1] = card
        await app.query_one("#request-grid").mount(card)
        app.last_snapshot = {"total_tokens": 100}
        await app._reset_run_view()
        await pilot.pause()
        assert app.cards == {}
        assert app.last_snapshot == {}
        assert len(app.query(".request-card")) == 0
