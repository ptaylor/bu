"""CLI entry point for bu — the backup utility.

Usage:
    bu <ACTION> <DESTINATION> [EXTRA...]

Actions:
    backup    Back up source paths to the destination
    restore   Restore data from the destination to a local path
    check     Show what files would be backed up
    verify    Verify integrity of backed-up data
    status    Show status of the backup destination
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import click

from bu import __version__
from bu.actions import (
    action_backup,
    action_check,
    action_restore,
    action_status,
    action_verify,
    format_result,
)
from bu.config import Config, ConfigError


class OrderedGroup(click.Group):
    """A Click group that lists commands in definition order."""

    def list_commands(self, ctx: click.Context) -> list[str]:
        return list(self.commands.keys())


# Shared options
_DRY_RUN = click.option("--dry-run", "-n", is_flag=True, help="Show what would be done without making changes.")
_JSON = click.option("--json", "json_output", is_flag=True, help="Output results as JSON.")
_CONFIG = click.option(
    "--config", "-c", "config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to config file (default: ~/.config/bu/config.toml).",
)
_DEST_ARG = click.argument("destination", metavar="<DESTINATION>")


def _load_config(config_path: Path | None) -> Config:
    """Load configuration, printing a friendly error and exiting on failure."""
    try:
        return Config(config_path)
    except ConfigError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@click.group(cls=OrderedGroup, invoke_without_command=True)
@click.version_option(__version__, "-V", "--version")
@click.pass_context
def main(ctx: click.Context) -> None:
    """bu — Backup utility for managing backups to various backends.

    Run 'bu <ACTION> --help' for details on each action.
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        # Also list configured destinations
        try:
            cfg = Config()
            dests = cfg.list_destinations()
            if dests:
                click.echo(f"\nConfigured destinations: {', '.join(dests)}")
            else:
                click.echo("\nNo destinations configured. Create ~/.config/bu/config.toml")
        except ConfigError:
            click.echo("\nNo config file found. Create ~/.config/bu/config.toml")


@main.command()
@_CONFIG
@_DRY_RUN
@_JSON
@_DEST_ARG
@click.option("--source", "-s", "sources", multiple=True, help="Override source paths (can be repeated).")
def backup(
    config_path: Path | None,
    dry_run: bool,
    json_output: bool,
    destination: str,
    sources: tuple[str, ...],
) -> None:
    """Back up source paths to DESTINATION."""
    cfg = _load_config(config_path)
    dest = cfg.get(destination)

    extra_args: dict[str, Any] = {}
    if sources:
        extra_args["source_paths"] = list(sources)

    result = action_backup(dest, dry_run=dry_run, extra_args=extra_args or None)
    click.echo(format_result(result, json_output=json_output))

    # Exit non-zero if there were errors
    if result.get("errors"):
        sys.exit(1)


@main.command()
@_CONFIG
@_DRY_RUN
@_JSON
@_DEST_ARG
@click.option("--to", "-t", "restore_path", required=True, help="Local path to restore files to.")
def restore(
    config_path: Path | None,
    dry_run: bool,
    json_output: bool,
    destination: str,
    restore_path: str,
) -> None:
    """Restore data from DESTINATION to a local path."""
    cfg = _load_config(config_path)
    dest = cfg.get(destination)

    result = action_restore(dest, restore_path, dry_run=dry_run)
    click.echo(format_result(result, json_output=json_output))

    if result.get("errors"):
        sys.exit(1)


@main.command()
@_CONFIG
@_JSON
@_DEST_ARG
@click.option("--source", "-s", "sources", multiple=True, help="Override source paths (can be repeated).")
def check(
    config_path: Path | None,
    json_output: bool,
    destination: str,
    sources: tuple[str, ...],
) -> None:
    """Check what files would be backed up to DESTINATION."""
    cfg = _load_config(config_path)
    dest = cfg.get(destination)

    extra_args: dict[str, Any] = {}
    if sources:
        extra_args["source_paths"] = list(sources)

    result = action_check(dest, extra_args=extra_args or None)
    click.echo(format_result(result, json_output=json_output))


@main.command()
@_CONFIG
@_JSON
@_DEST_ARG
@click.option("--full", is_flag=True, help="Perform full checksum verification.")
def verify(
    config_path: Path | None,
    json_output: bool,
    destination: str,
    full: bool,
) -> None:
    """Verify integrity of backed-up data at DESTINATION."""
    cfg = _load_config(config_path)
    dest = cfg.get(destination)

    result = action_verify(dest, full=full)
    click.echo(format_result(result, json_output=json_output))

    if result.get("missing") or result.get("mismatched"):
        sys.exit(1)


@main.command()
@_CONFIG
@_JSON
@_DEST_ARG
def status(
    config_path: Path | None,
    json_output: bool,
    destination: str,
) -> None:
    """Show status of the backup at DESTINATION."""
    cfg = _load_config(config_path)
    dest = cfg.get(destination)

    result = action_status(dest)
    click.echo(format_result(result, json_output=json_output))
