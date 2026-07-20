from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse
from textual.app import App, ComposeResult
from textual.containers import Container
from textual.widgets import Input, OptionList, Static

from .config import Settings
from .models import (
    ModelProfile,
    ModelStore,
    ModelStoreFile,
    ProviderProfile,
    fetch_model_ids,
    models_endpoint,
    normalize_base_url,
)


@dataclass(frozen=True)
class SelectedProfile:
    provider: ProviderProfile
    model: ModelProfile


class ClipboardInput(Input):
    """Add paste shortcuts commonly used by Windows terminal users."""

    BINDINGS = list(Input.BINDINGS) + [
        ("ctrl+shift+v", "paste", "粘贴"),
        ("shift+insert", "paste", "粘贴"),
    ]


class ModelSetupApp(App[None]):
    """First-use and model-library setup wizard."""

    TITLE = "TokenFlow Setup"
    BINDINGS = [("escape", "back", "返回")]
    CSS = """
    Screen { background: #080808; color: #dedede; }
    #panel { width: 1fr; max-width: 100; height: 1fr; padding: 3 6; }
    #title { text-style: bold; color: #f0a94b; margin-bottom: 1; }
    #description { color: #bcbcbc; margin-bottom: 2; }
    #options { height: auto; max-height: 20; background: transparent; border: none; }
    #input { height: 3; display: none; }
    #input.visible { display: block; }
    #status { color: #bcbcbc; margin-top: 2; }
    #help { color: #858585; margin-top: 2; }
    """

    def __init__(
        self,
        settings: Settings,
        store_file: ModelStoreFile,
        store: ModelStore | None = None,
    ) -> None:
        super().__init__()
        self.settings = settings
        self.store_file = store_file
        self.store = store or store_file.load()
        self.result: SelectedProfile | None = None
        self.stage = "catalog"
        self.provider: ProviderProfile | None = None
        self.model_id = ""
        self.price_stage = "currency"
        self.model_ids: list[str] = []
        self.catalog_items: list[SelectedProfile] = []

    def compose(self) -> ComposeResult:
        with Container(id="panel"):
            yield Static("TokenFlow", id="title")
            yield Static("", id="description")
            yield OptionList(id="options")
            yield ClipboardInput(id="input")
            yield Static("", id="status")
            yield Static("上下键选择 · Enter 确认 · Esc 返回 · Ctrl+V / Ctrl+Shift+V 粘贴", id="help")

    async def on_mount(self) -> None:
        if self.store.providers and any(provider.models for provider in self.store.providers):
            self._show_catalog()
        elif self.store.providers:
            self._show_provider_choices()
        else:
            self._show_input("provider_name", "首次配置", "供应商名称", "例如 provider-name")

    def _show_input(
        self, stage: str, title: str, description: str, placeholder: str, *, password: bool = False
    ) -> None:
        self.stage = stage
        self.query_one("#title", Static).update(title)
        self.query_one("#description", Static).update(description)
        options = self.query_one("#options", OptionList)
        options.display = False
        input_widget = self.query_one("#input", Input)
        input_widget.display = True
        input_widget.add_class("visible")
        input_widget.placeholder = placeholder
        input_widget.value = ""
        input_widget.password = password
        input_widget.focus()
        self.query_one("#status", Static).update("")

    def _show_options(self, stage: str, title: str, description: str, choices: list[str]) -> None:
        self.stage = stage
        self.query_one("#title", Static).update(title)
        self.query_one("#description", Static).update(description)
        options = self.query_one("#options", OptionList)
        options.clear_options()
        options.add_options(choices)
        options.highlighted = 0
        options.display = True
        input_widget = self.query_one("#input", Input)
        input_widget.remove_class("visible")
        input_widget.display = False
        self.query_one("#status", Static).update("")
        options.focus()

    def _show_catalog(self) -> None:
        self.catalog_items = [
            SelectedProfile(provider, model)
            for provider in self.store.providers
            for model in provider.models
        ]
        choices = [f"{item.provider.name} / {item.model.id}" for item in self.catalog_items]
        choices.extend(["添加模型", "添加供应商"])
        self._show_options(
            "catalog",
            "选择供应商和模型",
            "每次运行使用一个模型；价格来自模型库",
            choices,
        )

    def _begin_provider(self, *, provider: ProviderProfile | None = None) -> None:
        self.provider = provider
        if provider is None:
            self._show_input("provider_name", "添加供应商", "供应商名称", "例如 provider-name")
        else:
            self._show_model_source()

    def _show_model_source(self) -> None:
        if self.provider is None:
            return
        self._show_options(
            "model_source",
            "模型 ID",
            f"供应商：{self.provider.name} · {self.provider.base_url}",
            ["从 /models 列表选择", "手动填写模型 ID", "返回"],
        )

    async def _fetch_models(self) -> None:
        if self.provider is None:
            return
        self.query_one("#status", Static).update(
            f"正在请求 {models_endpoint(self.provider.base_url)} ..."
        )
        try:
            self.model_ids = await fetch_model_ids(
                self.provider.base_url,
                self.provider.api_key,
                timeout=max(5, self.settings.first_token_timeout),
            )
        except ValueError as exc:
            self.query_one("#status", Static).update(str(exc) + "；可返回手动填写")
            return
        self._show_model_list()

    def _show_model_list(self) -> None:
        self._show_options(
            "model_list",
            "选择模型",
            f"已从 {self.provider.name if self.provider else ''} 获取 {len(self.model_ids)} 个模型",
            self.model_ids + ["刷新列表", "手动填写模型 ID", "返回"],
        )

    def _show_price(self, stage: str | None = None) -> None:
        self.price_stage = stage or "currency"
        prompts = {
            "currency": ("模型价格", "币种代码，可直接回车使用 USD", "例如 USD", False),
            "input_price": ("输入价格", "每百万输入 Token 的价格，可直接回车使用 0", "例如 0.75", False),
            "output_price": ("输出价格", "每百万输出 Token 的价格，可直接回车使用 0", "例如 4.5", False),
            "multiplier": ("价格倍率", "可选，可直接回车使用 1", "默认 1", False),
        }
        title, description, placeholder, password = prompts[self.price_stage]
        self._show_input(self.price_stage, title, description, placeholder, password=password)

    @staticmethod
    def _valid_decimal(value: str, *, default: str | None = None) -> str | None:
        value = value.strip() or (default or "")
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            return None
        if not parsed.is_finite() or parsed < 0:
            return None
        return value

    def _save_model(self) -> None:
        if self.provider is None or not self.model_id:
            return
        model = ModelProfile(
            id=self.model_id,
            currency=getattr(self, "pending_currency", "USD"),
            input_per_million=getattr(self, "pending_input", "0"),
            output_per_million=getattr(self, "pending_output", "0"),
            multiplier=getattr(self, "pending_multiplier", "1"),
        )
        self.store.add_provider(self.provider)
        self.provider = self.store.find_provider(self.provider.base_url)
        assert self.provider is not None
        self.store.add_model(self.provider, model)
        self.store_file.save(self.store)
        self.result = SelectedProfile(self.provider, model)
        self.exit()

    @staticmethod
    def _valid_base_url(value: str) -> bool:
        parsed = urlparse(value.strip())
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    async def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        index = event.option_index
        if self.stage == "catalog":
            if index < len(self.catalog_items):
                self.result = self.catalog_items[index]
                self.exit()
            elif index == len(self.catalog_items):
                if len(self.store.providers) == 1:
                    self._begin_provider(provider=self.store.providers[0])
                else:
                    self._show_provider_choices()
            else:
                self._begin_provider()
        elif self.stage == "provider_choices":
            if index < len(self.store.providers):
                self._begin_provider(provider=self.store.providers[index])
            else:
                self._show_catalog()
        elif self.stage == "model_source":
            if index == 0:
                await self._fetch_models()
            elif index == 1:
                self._show_input("model_id", "手动填写模型 ID", "输入 API 支持的模型名称", "例如 model-id")
            else:
                self._show_catalog()
        elif self.stage == "model_list":
            if index < len(self.model_ids):
                self.model_id = self.model_ids[index]
                self._show_price()
            elif index == len(self.model_ids):
                await self._fetch_models()
            elif index == len(self.model_ids) + 1:
                self._show_input("model_id", "手动填写模型 ID", "输入 API 支持的模型名称", "例如 model-id")
            else:
                self._show_model_source()

    def _show_provider_choices(self) -> None:
        self._show_options(
            "provider_choices",
            "选择供应商",
            "选择要添加模型的供应商",
            [provider.name for provider in self.store.providers] + ["返回"],
        )

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if self.stage == "provider_name":
            if not value:
                self.notify("供应商名称不能为空", severity="error")
                return
            self.provider = ProviderProfile(value, "", "")
            self._show_input("base_url", "Base URL", "OpenAI-compatible API 地址", "https://api.example.com/v1")
        elif self.stage == "base_url":
            if not self._valid_base_url(value):
                self.notify("Base URL 必须是包含主机名的 http:// 或 https:// 地址", severity="error")
                return
            assert self.provider is not None
            self.provider.base_url = normalize_base_url(value)
            self._show_input("api_key", "API Key", "Key 会保存在用户目录 models.json 中", "sk-...", password=True)
        elif self.stage == "api_key":
            if not value:
                self.notify("API Key 不能为空", severity="error")
                return
            assert self.provider is not None
            self.provider.api_key = value
            self._show_model_source()
        elif self.stage == "model_id":
            if not value:
                self.notify("模型 ID 不能为空", severity="error")
                return
            self.model_id = value
            self._show_price()
        elif self.stage == "currency":
            if len(value) > 12:
                self.notify("币种代码不能超过 12 个字符", severity="error")
                return
            self.pending_currency = value.upper() or "USD"
            self._show_price("input_price")
        elif self.stage == "input_price":
            parsed = self._valid_decimal(value, default="0")
            if parsed is None:
                self.notify("请输入大于等于 0 的数字", severity="error")
                return
            self.pending_input = parsed
            self._show_price("output_price")
        elif self.stage == "output_price":
            parsed = self._valid_decimal(value, default="0")
            if parsed is None:
                self.notify("请输入大于等于 0 的数字", severity="error")
                return
            self.pending_output = parsed
            self._show_price("multiplier")
        elif self.stage == "multiplier":
            parsed = self._valid_decimal(value, default="1")
            if parsed is None:
                self.notify("倍率必须是大于等于 0 的数字", severity="error")
                return
            self.pending_multiplier = parsed
            self._save_model()

    def action_back(self) -> None:
        if self.stage == "catalog":
            self.exit()
        elif self.stage == "provider_choices":
            self._show_catalog()
        elif self.stage == "model_source":
            self._show_catalog()
        elif self.stage == "model_list":
            self._show_model_source()
        elif self.stage == "provider_name":
            self.exit()
        elif self.stage == "base_url":
            self._show_input("provider_name", "供应商名称", "供应商名称", "例如 provider-name")
        elif self.stage == "api_key":
            self._show_input("base_url", "Base URL", "OpenAI-compatible API 地址", "https://api.example.com/v1")
        elif self.stage == "model_id":
            self._show_model_source()
        elif self.stage == "currency":
            self._show_model_source()
        elif self.stage == "input_price":
            self._show_price("currency")
        elif self.stage == "output_price":
            self._show_price("input_price")
        elif self.stage == "multiplier":
            self._show_price("output_price")
