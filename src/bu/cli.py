"""CLI entry point for bu — the backup utility.

Usage:
    bu <ACTION> <DESTINATION> [EXTRA...]

Actions:
    backup    Back up source paths to the destination
    restore   Restore data from the destination to a local path
    check     Show what files would be backed up
    verify    Verify integrity of backed-up data
    status    Show status of the backup destination
    config    Edit the configuration file in $EDITOR
    history   Show action history for a destination
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
    action_config,
    action_history,
    action_restore,
    action_status,
    action_verify,
    format_history,
    format_result,
    format_status,
)
from bu.config import Config, ConfigError, DestinationConfig


class OrderedGroup(click.Group):
    """A Click group that lists commands in definition order."""

    def list_commands(self, ctx: click.Context) -> list[str]:
        return list(self.commands.keys())


# Shared options
_DRY_RUN = click.option("--dry-run", "-n", is_flag=True, help="Show what would be done without making changes.")
_JSON = click.option("--json", "json_output", is_flag=True, help="Output results as JSON.")
_CONFIG = click.option(
    "--config", "-c", "config_dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Path to config directory (default: ~/.config/bu/).",
)
_CONFIG_OPTIONAL = click.option(
    "--config", "-c", "config_dir", type=click.Path(file_okay=False, path_type=Path),
    help="Path to config directory (default: ~/.config/bu/).",
)
_DEST_ARG = click.argument("destination", metavar="<DESTINATION>")


def _load_config(config_dir: Path | None) -> Config:
    """Load configuration, printing a friendly error and exiting on failure."""
    try:
        return Config(config_dir)
    except ConfigError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


def _resolve_destination(config_dir: Path | None, destination: str) -> DestinationConfig:
    """Load config and resolve a destination, with a friendly error on failure."""
    cfg = _load_config(config_dir)
    try:
        return cfg.get(destination)
    except ConfigError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@click.group(cls=OrderedGroup, invoke_without_command=True)
@click.version_option(__version__, "-V", "--version")
@click.pass_context
def main(ctx: click.Context) -> None:
    """bu — Backup utility for managing backups via rsync or duplicity.

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
                click.echo("\nNo destinations configured. Run 'bu config <name> --method <type>' to create one.")
        except ConfigError:
            click.echo("\nNo config directory found. Run 'bu config <name> --method <type>' to get started.")


@main.command()
@_CONFIG
@_DRY_RUN
@_JSON
@_DEST_ARG
@click.option("--source", "-s", "sources", multiple=True, help="Override source paths (can be repeated).")
def backup(
    config_dir: Path | None,
    dry_run: bool,
    json_output: bool,
    destination: str,
    sources: tuple[str, ...],
) -> None:
    """Back up source paths to DESTINATION."""
    dest = _resolve_destination(config_dir, destination)

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
    config_dir: Path | None,
    dry_run: bool,
    json_output: bool,
    destination: str,
    restore_path: str,
) -> None:
    """Restore data from DESTINATION to a local path."""
    dest = _resolve_destination(config_dir, destination)

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
    config_dir: Path | None,
    json_output: bool,
    destination: str,
    sources: tuple[str, ...],
) -> None:
    """Check what files would be backed up to DESTINATION."""
    dest = _resolve_destination(config_dir, destination)

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
    config_dir: Path | None,
    json_output: bool,
    destination: str,
    full: bool,
) -> None:
    """Verify integrity of backed-up data at DESTINATION."""
    dest = _resolve_destination(config_dir, destination)

    result = action_verify(dest, full=full)
    click.echo(format_result(result, json_output=json_output))

    if result.get("missing") or result.get("mismatched"):
        sys.exit(1)


@main.command()
@_CONFIG
@_JSON
@_DEST_ARG
def status(
    config_dir: Path | None,
    json_output: bool,
    destination: str,
) -> None:
    """Show status of the backup at DESTINATION."""
    dest = _resolve_destination(config_dir, destination)

    result = action_status(dest)
    click.echo(format_status(result, json_output=json_output))


@main.command()
@_CONFIG_OPTIONAL
@_JSON
@click.argument("destination", metavar="<DESTINATION>", required=False, default=None)
@click.option(
    "--method", "-m", "method",
    type=click.Choice(["rsync", "duplicity"]),
    help="Backup method for the sample template when creating a new destination.",
)
def config(
    config_dir: Path | None,
    json_output: bool,
    destination: str | None,
    method: str | None,
) -> None:
    """Edit a destination configuration in $EDITOR.

    If <DESTINATION> is given, opens (or creates) its .toml file.
    Use --method to get a tailored sample template for new destinations.
    Without <DESTINATION>, lists all configured destinations.
    """
    result = action_config(config_dir, destination=destination, method=method)
    click.echo(format_result(result, json_output=json_output))

    if not result.get("ok"):
        sys.exit(1)


@main.command()
@_CONFIG
@_JSON
@_DEST_ARG
@click.option("--limit", "-n", type=int, default=0, help="Show only the last N actions.")
def history(
    config_dir: Path | None,
    json_output: bool,
    destination: str,
    limit: int,
) -> None:
    """Show the action history for DESTINATION.

    Displays backup and restore actions in reverse chronological order
    (latest first). Each action shows start/end timestamps, file/size
    stats, and any errors or notes recorded during the run.
    """
    dest = _resolve_destination(config_dir, destination)

    result = action_history(dest, limit=limit)
    click.echo(format_history(result, json_output=json_output))
