"""Development wrapper; installed users should run the ``tokenflow`` command."""

from tokenflow_cli.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
