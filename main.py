#!/usr/bin/env python3
"""Zelos CAN extension - CAN bus monitoring and database decoding."""

import logging
from pathlib import Path

import rich_click as click
from zelos_sdk.hooks.logging import TraceLoggingHandler

from zelos_extension_can import cli as cli_commands

DEMO_DBC_PATH = Path(__file__).parent / "zelos_extension_can" / "demo" / "demo.dbc"

# Configure rich-click
click.rich_click.USE_RICH_MARKUP = True
click.rich_click.USE_MARKDOWN = True
click.rich_click.SHOW_ARGUMENTS = True
click.rich_click.GROUP_ARGUMENTS_OPTIONS = True
click.rich_click.STYLE_ERRORS_SUGGESTION = "yellow italic"

# Configure logging - INFO level prevents debug logs from being sent to backend
logging.basicConfig(level=logging.INFO)

# Add the built-in handler to capture logs at INFO level and above
# (DEBUG logs won't be sent to backend to avoid duplicate trace data)
handler = TraceLoggingHandler("can_log")
handler.setLevel(logging.INFO)
logging.getLogger().addHandler(handler)


@click.group(invoke_without_command=True)
@click.option(
    "--demo",
    is_flag=True,
    help="Run in demo mode with built-in EV simulator",
)
@click.option(
    "--file",
    type=click.Path(path_type=Path),
    default=None,
    is_flag=False,
    flag_value=".",
    help="Record trace to .trz file (defaults to UTC.trz if no filename specified)",
)
@click.pass_context
def cli(ctx: click.Context, demo: bool, file: Path | None) -> None:
    """CAN bus monitoring and database decoding.

    Traces a CAN bus given an interface, channel, database file, bitrate, etc.
    Configure via Zelos extension settings or use --demo for testing.
    """
    # If a subcommand was invoked, don't run the main trace logic
    if ctx.invoked_subcommand is not None:
        return

    # Run app-based configuration mode
    cli_commands.run_app_mode(demo, file, DEMO_DBC_PATH)


# Register subcommands
cli.add_command(cli_commands.trace)
cli.add_command(cli_commands.convert)
cli.add_command(cli_commands.export)


if __name__ == "__main__":
    cli()
