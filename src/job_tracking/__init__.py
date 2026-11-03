"""Hourly job-posting tracker that writes new postings to a styled Excel sheet."""


def main() -> None:
    from .cli import main as cli_main

    raise SystemExit(cli_main())
