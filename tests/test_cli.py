from pathlib import Path

from tokenflow_cli.cli import initialize
from tokenflow_cli.tokenizer import count_tokens


def test_initialize_creates_safe_local_workspace(tmp_path: Path) -> None:
    created = initialize(tmp_path, force=False, generate_prompt=True)
    assert set(path.name for path in created) == {
        "config.yaml",
        ".env.example",
        "prompt.txt",
    }
    config = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    env_example = (tmp_path / ".env.example").read_text(encoding="utf-8")
    assert "base_url:" not in config
    assert "model:" not in config
    assert "TOKENFLOW_API_KEY" in env_example
    assert "sk-" not in config and "sk-" not in env_example
    prompt = (tmp_path / "prompt.txt").read_text(encoding="utf-8")
    assert count_tokens(prompt) >= 20000


def test_initialize_does_not_overwrite_without_force(tmp_path: Path) -> None:
    initialize(tmp_path, force=False, generate_prompt=True)
    config = tmp_path / "config.yaml"
    config.write_text("local: true\n", encoding="utf-8")
    initialize(tmp_path, force=False, generate_prompt=False)
    assert config.read_text(encoding="utf-8") == "local: true\n"
