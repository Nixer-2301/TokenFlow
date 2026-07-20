"""Development wrapper for the packaged TokenFlow prompt generator."""

from tokenflow_cli.prompt_generator import build_prompt, main

__all__ = ["build_prompt", "main"]


if __name__ == "__main__":
    main()
