"""Output routing for myharem.

Convention: **stdout is the result, stderr is progress/diagnostics.**

- Human progress, status dots, warnings, and successes go to **stderr** via the
  helpers here (in both human and JSON modes), so stdout is never polluted.
- The final command *result* is written to **stdout** by the CLI layer
  (`cli.py`): a JSON object when `--json` is set, otherwise a human summary.

This lets an automation caller (e.g. the MSRS control plane) capture stdout as
structured JSON and stderr as a log stream, while humans still see progress.
"""

import click

_json_mode = False


def set_json_mode(enabled: bool) -> None:
    global _json_mode
    _json_mode = bool(enabled)


def json_mode() -> bool:
    return _json_mode


def log(message: str, fg: str | None = None, bold: bool = False,
        nl: bool = True) -> None:
    """Progress/diagnostic line → stderr."""
    click.secho(message, fg=fg, bold=bold, nl=nl, err=True)


def success(message: str) -> None:
    click.secho(message, fg="green", err=True)


def warn(message: str) -> None:
    click.secho(message, fg="yellow", err=True)


def error(message: str) -> None:
    click.secho(message, fg="red", bold=True, err=True)
