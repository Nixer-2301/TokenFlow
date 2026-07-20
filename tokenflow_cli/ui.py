from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Container, Grid, Horizontal, Vertical
from textual.widgets import Input, OptionList, Static

from . import __version__
from .client import OpenAICompatibleClient
from .config import Settings
from .request_log import JsonlLogger
from .scheduler import Scheduler
from .state import load_last_selection, save_last_selection
from .stats import RequestRecord
from .targets import RunTarget

VERSION = __version__


def mask_api_key(value: str) -> str:
    if len(value) <= 8:
        return value[:2] + "***"
    return f"{value[:5]}***{value[-3:]}"


def compact_number(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


@dataclass
class LaunchSelection:
    target: RunTarget
    model: str


class ClipboardInput(Input):
    """Add paste shortcuts commonly used by Windows terminal users."""

    BINDINGS = list(Input.BINDINGS) + [
        ("ctrl+shift+v", "paste", "粘贴"),
        ("shift+insert", "paste", "粘贴"),
    ]


class RequestCard(Static):
    def __init__(self, request_id: int, model: str) -> None:
        super().__init__(classes="request-card")
        self.request_id = request_id
        self.model = model
        self.status = "queued"
        self.tokens = 0
        self.spent_microcents = 0
        self.first_text: float | None = None

    def update_state(self, data: dict[str, object], currency: str) -> None:
        self.status = str(data.get("status", self.status))
        self.tokens = int(data.get("total_tokens", self.tokens))
        self.spent_microcents = int(
            data.get("spent_microcents", self.spent_microcents)
        )
        elapsed = data.get("elapsed")
        if isinstance(elapsed, (int, float)):
            self.first_text = float(elapsed)
        spend = self.spent_microcents / 100_000_000
        lines = [
            f"请求 {self.request_id}",
            self.model,
            f"{compact_number(self.tokens)} Tokens",
            f"{currency} {spend:.6f}",
            f"状态: {self.status}",
        ]
        if self.first_text is not None:
            lines.append(f"首字符: {self.first_text:.2f}s")
        self.update("\n".join(lines))
        self.remove_class("success", "failed", "streaming")
        if self.status in {"success", "failed", "streaming"}:
            self.add_class(self.status)


class SetupPanel(Vertical):
    def compose(self) -> ComposeResult:
        yield Static("运行设置", id="question-title")
        yield Static("", id="question-description")
        yield OptionList(id="question-options")
        yield ClipboardInput(id="custom-input", placeholder="输入后按 Enter 确认")
        yield Static("↑↓ 选择   Enter 确认   Esc 返回   Ctrl+V 粘贴", id="question-help")


class TokenFlowApp(App[None]):
    TITLE = "TokenFlow"
    CSS = """
    Screen {
        background: #080808;
        color: #dedede;
    }
    #body {
        height: 1fr;
    }
    #main-area {
        width: 1fr;
        height: 1fr;
        border-right: solid #4a4a4a;
    }
    #request-scroll {
        width: 1fr;
        height: 1fr;
        overflow-y: auto;
        padding: 1 2;
    }
    #request-grid {
        width: 1fr;
        height: auto;
        grid-size: 2;
        grid-gutter: 1 1;
    }
    .request-card {
        height: 9;
        min-width: 22;
        padding: 1 2;
        border: solid #343434;
        background: #101010;
    }
    .request-card.streaming { border: solid #d98a22; }
    .request-card.success { border: solid #388a54; }
    .request-card.failed { border: solid #a94d4d; }
    #sidebar {
        width: 34;
        min-width: 28;
        height: 1fr;
        padding: 1 2;
        background: #111111;
    }
    #brand {
        text-style: bold;
        margin-bottom: 2;
    }
    #summary {
        height: auto;
    }
    #connection {
        height: auto;
        margin-top: 2;
    }
    #version {
        dock: bottom;
        color: #bcbcbc;
        margin-bottom: 1;
    }
    #setup {
        height: 16;
        border-top: solid #4a4a4a;
        background: #121212;
        padding: 1 2;
    }
    #question-title { text-style: bold; }
    #question-description { color: #bcbcbc; margin-bottom: 1; }
    #question-options {
        height: 6;
        background: transparent;
        border: none;
    }
    #custom-input {
        display: none;
        height: 3;
    }
    #custom-input.visible { display: block; }
    #question-help { color: #858585; margin-top: 1; }
    #run-footer {
        display: none;
        height: 3;
        border-top: solid #4a4a4a;
        padding: 1 2;
        background: #121212;
    }
    #run-footer.visible { display: block; }
    """
    BINDINGS = [
        ("ctrl+c", "stop_run", "停止"),
        ("escape", "back", "返回"),
    ]

    def __init__(
        self,
        settings: Settings,
        prompt: str,
        prompt_tokens: int,
        state_path: Path,
        preset: LaunchSelection | None = None,
        model_locked: bool = False,
    ) -> None:
        super().__init__()
        self.settings = settings
        self.prompt = prompt
        self.prompt_tokens = prompt_tokens
        self.state_path = state_path
        self.last = load_last_selection(state_path)
        self.selection = preset
        self.model_locked = model_locked
        self.stage = "target_source"
        self.pending_mode = "requests"
        self.pending_target: RunTarget | None = None
        self.pending_model = settings.model
        self.cards: dict[int, RequestCard] = {}
        self.scheduler: Scheduler | None = None
        self.run_task: asyncio.Task[None] | None = None
        self.last_snapshot: dict[str, int | float] = {}
        self.run_stop_reason = "运行完成"

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            with Vertical(id="main-area"):
                with Container(id="request-scroll"):
                    yield Grid(id="request-grid")
                yield SetupPanel(id="setup")
                yield Static("", id="run-footer")
            with Vertical(id="sidebar"):
                yield Static("TokenFlow", id="brand")
                yield Static("", id="summary")
                yield Static("", id="connection")
                yield Static(f"TokenFlow {VERSION}", id="version")

    async def on_mount(self) -> None:
        self._update_sidebar()
        self.call_after_refresh(self._resize_grid)
        if self.selection is not None:
            await self._start_run(self.selection)
        else:
            self._show_target_source()

    def on_resize(self) -> None:
        self._resize_grid()

    def _resize_grid(self) -> None:
        try:
            # Screen width is available before child layout settles, avoiding
            # a one-column flash when Textual mounts the initial screen.
            width = max(1, self.size.width - 34)
            columns = 1 if width < 55 else 2 if width < 85 else 3 if width < 115 else 4
            grid = self.query_one("#request-grid", Grid)
            grid.styles.grid_size_columns = columns
            grid.styles.grid_columns = "1fr " * columns
        except Exception:
            return

    def _set_question(self, title: str, description: str, options: list[str]) -> None:
        self.query_one("#question-title", Static).update(title)
        self.query_one("#question-description", Static).update(description)
        option_list = self.query_one("#question-options", OptionList)
        option_list.clear_options()
        option_list.add_options(options)
        option_list.highlighted = 0
        option_list.display = True
        custom = self.query_one("#custom-input", Input)
        custom.remove_class("visible")
        custom.value = ""
        option_list.focus()

    def _show_target_source(self) -> None:
        self.stage = "target_source"
        mode = str(self.last.get("mode", "requests"))
        value = self.last.get("target", self.settings.max_requests)
        label = "无限运行" if mode == "unlimited" else f"{mode}: {value}"
        self._set_question(
            "运行目标",
            "选择上次设定，或输入你自己的设定",
            [f"使用上次选择：{label}", "输入自己的设定"],
        )

    def _show_mode(self) -> None:
        self.stage = "mode"
        self._set_question(
            "停止方式",
            "达到目标后停止补位；无限模式由 Ctrl+C、熔断或空闲保护停止",
            ["请求总次数", "总 Token 用量", "无限运行"],
        )

    def _show_value_input(self) -> None:
        self.stage = "target_value"
        options = self.query_one("#question-options", OptionList)
        options.display = False
        description = "请输入请求总次数" if self.pending_mode == "requests" else "请输入总 Token 目标"
        self.query_one("#question-title", Static).update("目标数值")
        self.query_one("#question-description", Static).update(description)
        custom = self.query_one("#custom-input", Input)
        custom.add_class("visible")
        custom.placeholder = "输入正整数后按 Enter"
        custom.focus()

    def _show_model_source(self) -> None:
        self.stage = "model_source"
        last_model = str(self.last.get("model", self.settings.model))
        self._set_question(
            "模型",
            "本次运行只使用一个模型",
            [f"使用上次选择：{last_model}", "输入自己的模型"],
        )

    def _show_model_input(self) -> None:
        self.stage = "model_value"
        self.query_one("#question-options", OptionList).display = False
        self.query_one("#question-title", Static).update("自定义模型")
        self.query_one("#question-description", Static).update("输入 API 支持的模型名称")
        custom = self.query_one("#custom-input", Input)
        custom.add_class("visible")
        custom.placeholder = self.settings.model
        custom.focus()

    def _show_confirmation(self) -> None:
        self.stage = "confirm"
        target = self.pending_target or RunTarget.requests(self.settings.max_requests)
        self._set_question(
            "确认运行",
            f"{target.label()} · {self.pending_model} · concurrency {self.settings.concurrency}",
            ["开始运行", "返回修改"],
        )

    async def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        index = event.option_index
        if self.stage == "target_source":
            if index == 0:
                self.pending_target = self._target_from_last()
                self._continue_after_target()
            else:
                self._show_mode()
        elif self.stage == "mode":
            if index == 2:
                self.pending_target = RunTarget.unlimited()
                self._continue_after_target()
            else:
                self.pending_mode = "requests" if index == 0 else "total_tokens"
                self._show_value_input()
        elif self.stage == "model_source":
            if index == 0:
                self.pending_model = str(self.last.get("model", self.settings.model))
                self._show_confirmation()
            else:
                self._show_model_input()
        elif self.stage == "confirm":
            if index == 0:
                selection = LaunchSelection(
                    self.pending_target or RunTarget.requests(self.settings.max_requests),
                    self.pending_model,
                )
                await self._start_run(selection)
            else:
                self._show_target_source()
        elif self.stage == "complete":
            if index == 0 and self.selection is not None:
                await self._start_run(self.selection)
            elif index == 1:
                self._show_target_source()
            else:
                self.exit()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if self.stage == "target_value":
            try:
                number = int(value.replace(",", ""))
                self.pending_target = (
                    RunTarget.requests(number)
                    if self.pending_mode == "requests"
                    else RunTarget.total_tokens(number)
                )
            except ValueError:
                self.notify("请输入大于 0 的整数", severity="error")
                return
            self._continue_after_target()
        elif self.stage == "model_value":
            if not value:
                self.notify("模型名称不能为空", severity="error")
                return
            self.pending_model = value
            self._show_confirmation()

    def _continue_after_target(self) -> None:
        if self.model_locked:
            self.pending_model = self.settings.model
            self._show_confirmation()
        else:
            self._show_model_source()

    async def _start_run(self, selection: LaunchSelection) -> None:
        await self._reset_run_view()
        self.selection = selection
        self.run_stop_reason = "运行完成"
        save_last_selection(
            self.state_path,
            {
                "mode": selection.target.mode,
                "target": selection.target.value,
                "model": selection.model,
            },
        )
        self.query_one("#setup").display = False
        footer = self.query_one("#run-footer", Static)
        footer.add_class("visible")
        footer.update(
            f"运行中 · {selection.target.label()} · Ctrl+C 停止 · 等待请求状态..."
        )
        self._update_sidebar()
        self.run_task = asyncio.create_task(self._run_scheduler(selection))

    async def _run_scheduler(self, selection: LaunchSelection) -> None:
        logger = JsonlLogger(self.settings.logs_dir)
        try:
            async with OpenAICompatibleClient(
                endpoint=self.settings.endpoint,
                api_key=self.settings.api_key,
                model=selection.model,
                max_tokens=self.settings.max_tokens,
                temperature=self.settings.temperature,
                timeout=self.settings.request_timeout,
                first_token_timeout=self.settings.first_token_timeout,
                max_retries=self.settings.max_retries,
                backoff_base_seconds=self.settings.backoff_base_seconds,
                extra_body=self.settings.extra_body,
            ) as client:
                self.scheduler = Scheduler(
                    client=client,
                    prompt=self.prompt,
                    target=selection.target,
                    concurrency=self.settings.concurrency,
                    circuit_breaker_threshold=self.settings.circuit_breaker_threshold,
                    minimum_completion_tokens=self.settings.minimum_completion_tokens,
                    logger=logger,
                    prompt_tokens=self.prompt_tokens,
                    pricing=self.settings.pricing,
                    global_idle_timeout=self.settings.global_idle_timeout,
                    on_update=self._on_scheduler_update,
                    model=selection.model,
                )
                stats = await self.scheduler.run()
                self.last_snapshot = await stats.snapshot()
                if self.run_stop_reason == "运行完成":
                    self.run_stop_reason = self._target_completion_reason(selection.target)
        except asyncio.CancelledError:
            self.run_stop_reason = "运行已取消"
        except (httpx.HTTPError, OSError, ValueError) as exc:
            self.run_stop_reason = f"运行错误：{exc}"
            self.notify(str(exc), severity="error", timeout=10)
        except Exception as exc:
            self.run_stop_reason = f"运行错误：{type(exc).__name__}"
            self.notify(
                f"{type(exc).__name__}: {exc}", severity="error", timeout=10
            )
        finally:
            logger.close()
            self.scheduler = None
            self.run_task = None
            self._update_sidebar()
            self._show_completion()

    async def _on_scheduler_update(
        self, kind: str, request_id: int | None, data: dict[str, object]
    ) -> None:
        if request_id is not None:
            card = self.cards.get(request_id)
            if card is None and kind == "started":
                card = RequestCard(request_id, str(data.get("model", self.settings.model)))
                self.cards[request_id] = card
                await self.query_one("#request-grid", Grid).mount(card)
                card.update_state({"status": "queued"}, self.settings.pricing.currency)
            if card is not None:
                if kind == "completed" and isinstance(data.get("record"), RequestRecord):
                    record = data["record"]
                    card.update_state(
                        {
                            "status": "success" if record.ok else "failed",
                            "total_tokens": record.total_tokens,
                            "spent_microcents": record.total_spend_microcents,
                        },
                        self.settings.pricing.currency,
                    )
                    snapshot = data.get("snapshot")
                    if isinstance(snapshot, dict):
                        self.last_snapshot = snapshot
                else:
                    card.update_state(data, self.settings.pricing.currency)
        if kind in {"idle_timeout", "circuit_breaker", "scheduler_error"}:
            reason = {
                "idle_timeout": "全局空闲保护触发",
                "circuit_breaker": "连续失败熔断",
                "scheduler_error": "调度器错误",
            }[kind]
            self.run_stop_reason = reason
            self.query_one("#run-footer", Static).update(reason)
        self._prune_cards()
        self._update_sidebar()

    def _prune_cards(self) -> None:
        limit = max(self.settings.concurrency * 2, 12)
        if len(self.cards) <= limit:
            return
        removable = [
            request_id
            for request_id, card in sorted(self.cards.items())
            if card.status in {"success", "failed", "cancelled"}
        ]
        for request_id in removable[: max(0, len(self.cards) - limit)]:
            card = self.cards.pop(request_id)
            card.remove()

    def _target_from_last(self) -> RunTarget:
        mode = str(self.last.get("mode", "requests"))
        value = self.last.get("target", self.settings.max_requests)
        if mode == "unlimited":
            return RunTarget.unlimited()
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = self.settings.max_requests
        return (
            RunTarget.total_tokens(number)
            if mode == "total_tokens"
            else RunTarget.requests(number)
        )

    def _update_sidebar(self) -> None:
        total_tokens = int(self.last_snapshot.get("total_tokens", 0))
        spent_microcents = int(self.last_snapshot.get("spent_microcents", 0))
        active = sum(
            card.status in {"queued", "starting", "streaming", "retrying"}
            for card in self.cards.values()
        )
        for card in self.cards.values():
            if card.status in {"queued", "starting", "streaming", "retrying"}:
                total_tokens += card.tokens
                spent_microcents += card.spent_microcents
        spent = spent_microcents / 100_000_000
        started = int(self.last_snapshot.get("started", 0))
        finished = int(self.last_snapshot.get("finished", 0))
        target = self.selection.target.label() if self.selection else "not started"
        self.query_one("#summary", Static).update(
            Text.from_markup(
                "当前数据\n"
                f"[bold]{total_tokens:,} tokens[/bold]\n"
                f"[bold]{self.settings.pricing.currency} {spent:.6f} spent[/bold]\n\n"
                f"活动请求\n{active} / {self.settings.concurrency}\n\n"
                f"请求进度\n{finished:,} completed / {started:,} started\n\n"
                f"运行目标\n{target}"
            )
        )
        model = self.selection.model if self.selection else self.settings.model
        self.query_one("#connection", Static).update(
            f"当前模型\n{model}\n\n"
            f"Base URL\n{self.settings.base_url}\n\n"
            f"API Key\n{mask_api_key(self.settings.api_key)}"
        )

    async def action_stop_run(self) -> None:
        if self.scheduler is not None:
            self.run_stop_reason = "用户手动停止"
            await self.scheduler.stop()
            self.query_one("#run-footer", Static).update("正在停止活动请求...")
        elif self.run_task is not None:
            self.run_stop_reason = "用户手动停止"
            self.run_task.cancel()
            self.query_one("#run-footer", Static).update("正在停止...")
        else:
            self.exit()

    def action_back(self) -> None:
        if self.run_task is not None:
            return
        if self.stage in {"mode", "target_value"}:
            self._show_target_source()
        elif self.stage in {"model_source", "model_value"}:
            self._show_mode()
        elif self.stage == "confirm":
            if self.model_locked:
                self._show_target_source()
            else:
                self._show_model_source()
        else:
            self.exit()

    async def _reset_run_view(self) -> None:
        await self.query_one("#request-grid", Grid).remove_children()
        self.cards.clear()
        self.last_snapshot = {}
        self.query_one("#setup").display = False
        footer = self.query_one("#run-footer", Static)
        footer.remove_class("visible")
        self._update_sidebar()

    def _show_completion(self) -> None:
        self.stage = "complete"
        footer = self.query_one("#run-footer", Static)
        footer.remove_class("visible")
        self.query_one("#setup").display = True
        total_tokens = int(self.last_snapshot.get("total_tokens", 0))
        successes = int(self.last_snapshot.get("successes", 0))
        failures = int(self.last_snapshot.get("failures", 0))
        self._set_question(
            "本次运行结束",
            (
                f"{self.run_stop_reason} · {total_tokens:,} Tokens · "
                f"成功 {successes} · 失败 {failures}"
            ),
            ["使用相同设置再次运行", "修改运行设置", "退出 TokenFlow"],
        )

    @staticmethod
    def _target_completion_reason(target: RunTarget) -> str:
        if target.mode == "requests":
            return "已达到请求次数目标"
        if target.mode == "total_tokens":
            return "已达到总 Token 目标"
        return "无限模式已停止"
