from __future__ import annotations

import argparse
import sys
from importlib.resources import files
from pathlib import Path

from .config import load_prompt, load_settings, settings_with_profile
from .errors import ConfigError
from .models import (
    ModelProfile,
    ModelStoreFile,
    ProviderProfile,
    user_data_dir,
)
from .onboarding import ModelSetupApp
from .prompt_generator import build_prompt
from .targets import RunTarget
from .ui import LaunchSelection, TokenFlowApp

PACKAGE_TEMPLATES = files("tokenflow_cli").joinpath("templates")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TokenFlow terminal interface")
    subparsers = parser.add_subparsers(dest="command")
    init = subparsers.add_parser("init", help="create local TokenFlow config files")
    init.add_argument("--directory", type=Path, default=Path.cwd())
    init.add_argument("--force", action="store_true", help="overwrite existing files")
    init.add_argument(
        "--generate-prompt", action="store_true", help="generate the long prompt"
    )

    parser.add_argument(
        "--config",
        type=Path,
        default=user_data_dir() / "config.yaml",
        help="运行配置路径，默认使用用户目录 ~/.tokenflow/config.yaml",
    )
    parser.add_argument("--model", help="override the model for this run")
    targets = parser.add_mutually_exclusive_group()
    targets.add_argument("--requests", type=int, help="stop after N logical requests")
    targets.add_argument(
        "--target-tokens", type=int, help="stop after usage.total_tokens reaches N"
    )
    targets.add_argument(
        "--unlimited", action="store_true", help="run until Ctrl+C or a safety stop"
    )
    parser.add_argument(
        "--generate-prompt",
        action="store_true",
        help="regenerate the long prompt before opening the TUI",
    )
    return parser


def _preset(args: argparse.Namespace, default_model: str, default_requests: int):
    target = None
    if args.requests is not None:
        target = RunTarget.requests(args.requests)
    elif args.target_tokens is not None:
        target = RunTarget.total_tokens(args.target_tokens)
    elif args.unlimited:
        target = RunTarget.unlimited()
    if target is None and args.model is None:
        return None
    return LaunchSelection(
        target or RunTarget.requests(default_requests), args.model or default_model
    )


def initialize(directory: Path, force: bool, generate_prompt: bool) -> list[Path]:
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    outputs = {
        "config.example.yaml": directory / "config.yaml",
        "env.example": directory / ".env.example",
    }
    created: list[Path] = []
    for resource_name, destination in outputs.items():
        if destination.exists() and not force:
            continue
        destination.write_bytes(
            PACKAGE_TEMPLATES.joinpath(resource_name).read_bytes()
        )
        created.append(destination)

    prompt_path = directory / "prompt.txt"
    if generate_prompt or not prompt_path.exists():
        prompt, _ = build_prompt()
        prompt_path.write_text(prompt, encoding="utf-8")
        created.append(prompt_path)
    return created


def _migrate_legacy_profile(settings, store_file: ModelStoreFile):
    """Import an old config's connection fields into the model library once."""
    if not settings.base_url or not settings.model or not settings.api_key:
        return None
    store = store_file.load()
    provider = store.find_provider(settings.base_url)
    if provider is None:
        provider = ProviderProfile(
            name="Imported provider",
            base_url=settings.base_url,
            api_key=settings.api_key,
        )
        store.providers.append(provider)
    if not any(model.id == settings.model for model in provider.models):
        provider.models.append(
            ModelProfile(
                id=settings.model,
                currency=settings.pricing.currency,
                input_per_million=str(settings.pricing.input_per_million_cents / 100),
                output_per_million=str(settings.pricing.output_per_million_cents / 100),
                multiplier=str(settings.pricing.multiplier),
            )
        )
    store_file.save(store)
    return next(model for model in provider.models if model.id == settings.model)


def _select_profile(settings, store_file: ModelStoreFile, requested_model: str | None):
    store = store_file.load()
    migrated = _migrate_legacy_profile(settings, store_file)
    if migrated is not None:
        store = store_file.load()

    if requested_model:
        matches = [
            (provider, model)
            for provider in store.providers
            for model in provider.models
            if model.id == requested_model
        ]
        if not matches:
            raise ConfigError(f"模型库中没有模型: {requested_model}")
        return matches[0]

    setup = ModelSetupApp(settings=settings, store_file=store_file, store=store)
    setup.run()
    if setup.result is None:
        raise ConfigError("未选择供应商和模型")
    return setup.result.provider, setup.result.model


def _run(args: argparse.Namespace) -> int:
    config_path = args.config.resolve()
    if not config_path.exists():
        initialize(config_path.parent, force=False, generate_prompt=True)
    settings = load_settings(config_path)
    if args.generate_prompt:
        prompt, _ = build_prompt(settings.min_input_tokens, settings.tokenizer)
        settings.prompt_file.write_text(prompt, encoding="utf-8")
    store_file = ModelStoreFile()
    provider, model = _select_profile(settings, store_file, args.model)
    settings = settings_with_profile(settings, provider, model)
    if not settings.api_key:
        raise ConfigError(f"供应商 {provider.name} 的 API Key 为空或环境变量未设置")
    prompt, prompt_tokens = load_prompt(settings)
    default_data_dir = user_data_dir().resolve()
    state_root = (
        default_data_dir
        if config_path.parent == default_data_dir
        else config_path.parent / ".tokenflow"
    )
    state_path = state_root / "last_selection.json"
    app = TokenFlowApp(
        settings=settings,
        prompt=prompt,
        prompt_tokens=prompt_tokens,
        state_path=state_path,
        preset=_preset(args, model.id, settings.max_requests),
        model_locked=True,
    )
    app.run()
    return 0


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "init":
            created = initialize(args.directory, args.force, args.generate_prompt)
            for path in created:
                print(f"Created {path}")
            if not created:
                print("Nothing changed; use --force to overwrite existing files")
            return 0
        return _run(args)
    except (ConfigError, RuntimeError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
