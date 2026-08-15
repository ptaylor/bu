"""CLI entry point for bu — the backup utility.

Usage:
    bu <ACTION> <DESTINATION> [EXTRA...]

Actions:
    backup    Back up source paths to the destination
    restore   Restore files into a local directory (never deletes)
    status    Show status and last backup details
    list      List all configured destinations
    config    Edit a destination configuration in $EDITOR
    delete    Delete a destination configuration file
    history   Show action history for a destination
    log       Print the raw execution log for a destination
    encrypt   Encrypt a secret (e.g. B2 credentials) for use in config
    create    Interactively create a new destination configuration
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click

from bu import __version__
from bu.actions import (
    action_backup,
    action_config,
    action_history,
    action_log,
    action_restore,
    action_status,
    format_history,
    format_result,
    format_status,
)
from bu.config import VALID_NAME_RE, Config, ConfigError, DestinationConfig


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


def _resolve_window(window_lines: int | None) -> int:
    """Resolve the live-output window height.

    Defaults to 18 lines when stdout is a TTY, 0 (plain text) otherwise.
    """
    if window_lines is not None:
        return window_lines
    return 18 if sys.stdout.isatty() else 0


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
@click.option("--window", "-w", "window_lines", type=int, default=None, help="Confine live output to an N-line window (default: 18 on a TTY, off when piped).")
def backup(
    config_dir: Path | None,
    dry_run: bool,
    json_output: bool,
    destination: str,
    sources: tuple[str, ...],
    window_lines: int | None,
) -> None:
    """Back up source paths to DESTINATION."""
    dest = _resolve_destination(config_dir, destination)

    extra_args: dict[str, Any] = {}
    if sources:
        extra_args["source_paths"] = list(sources)

    result = action_backup(
        dest,
        dry_run=dry_run,
        extra_args=extra_args or None,
        scroll_lines=_resolve_window(window_lines),
    )
    click.echo(format_result(result, json_output=json_output))

    # Exit non-zero if there were errors
    if result.get("errors"):
        sys.exit(1)


@main.command()
@_CONFIG
@_DRY_RUN
@_JSON
@_DEST_ARG
@click.argument("restore_dir", metavar="<RESTORE_DIR>")
@click.argument("path_within_backup", metavar="[PATH_WITHIN_BACKUP]", required=False, default=None)
@click.option("--window", "-w", "window_lines", type=int, default=None, help="Confine live output to an N-line window (default: 18 on a TTY, off when piped).")
def restore(
    config_dir: Path | None,
    dry_run: bool,
    json_output: bool,
    destination: str,
    restore_dir: str,
    path_within_backup: str | None,
    window_lines: int | None,
) -> None:
    """Restore files from DESTINATION into RESTORE_DIR.

    RESTORE_DIR must exist. PATH_WITHIN_BACKUP optionally narrows the
    restore to a subpath within the backup; its files are restored into
    RESTORE_DIR/<final component> (e.g. path 'x/y' restores into
    RESTORE_DIR/y). Restore never deletes files.
    """
    dest = _resolve_destination(config_dir, destination)

    result = action_restore(
        dest,
        restore_dir,
        path_within_backup,
        dry_run=dry_run,
        scroll_lines=_resolve_window(window_lines),
    )
    click.echo(format_result(result, json_output=json_output))

    if result.get("errors"):
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


@main.command("list")
@_JSON
def list_destinations(json_output: bool) -> None:
    """List all configured destinations.

    Shows every destination (backup config) with its method and target.
    """
    try:
        cfg = Config()
        dests = cfg.list_destinations()
    except ConfigError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    rows: list[dict[str, str]] = []
    for name in dests:
        try:
            d = cfg.get(name)
            rows.append({"name": name, "method": d.method, "destination": d.destination})
        except ConfigError as e:
            rows.append({"name": name, "method": "invalid", "destination": "", "error": str(e)})

    if json_output:
        click.echo(json.dumps(rows, indent=2))
        return

    if not rows:
        click.echo("No destinations configured.")
        click.echo("Run 'bu create' to configure one.")
        return

    for r in rows:
        if r.get("error"):
            click.echo(f"{r['name']:<24} <invalid config: {r['error']}>")
        else:
            click.echo(f"{r['name']:<24} {r['method']:<10} {r['destination']}")


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
@_CONFIG_OPTIONAL
@click.argument("destination", metavar="<DESTINATION>")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt.")
def delete(config_dir: Path | None, destination: str, yes: bool) -> None:
    """Delete the DESTINATION config file.

    Only the .toml config is removed — the backup contents at the
    destination are NOT deleted.
    """
    cfg = _load_config(config_dir)

    if not VALID_NAME_RE.match(destination):
        click.echo(
            f"Error: Invalid destination name {destination!r} — names may only "
            "contain letters, digits, '-' and '_'.",
            err=True,
        )
        sys.exit(1)

    file_path = cfg.config_dir / f"{destination}.toml"
    if not file_path.exists():
        available = ", ".join(cfg.list_destinations()) or "(none)"
        click.echo(
            f"Error: No config found for {destination!r}. "
            f"Available destinations: {available}",
            err=True,
        )
        sys.exit(1)

    # Best-effort: find where the backup contents live for the warning.
    dest_path: str | None = None
    try:
        dest_path = cfg.get(destination).destination
    except ConfigError:
        pass

    click.echo("Warning: this deletes only the config file — the backup contents are NOT deleted.")
    if not yes:
        try:
            confirmed = click.confirm(f"Delete config for {destination!r}?", default=False)
        except click.exceptions.Abort:
            click.echo("Aborted.")
            sys.exit(1)
        if not confirmed:
            click.echo("Aborted.")
            sys.exit(1)

    file_path.unlink()
    click.echo(f"Deleted: {file_path}")
    if dest_path:
        click.echo(f"Warning: backup contents at {dest_path!r} were NOT deleted.")
    else:
        click.echo("Warning: backup contents were NOT deleted.")


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


@main.command()
@_CONFIG
@_DEST_ARG
@click.option("--lines", "-n", type=int, default=0, help="Show only the last N lines.")
def log(
    config_dir: Path | None,
    destination: str,
    lines: int,
) -> None:
    """Print the raw execution log for DESTINATION to stdout."""
    dest = _resolve_destination(config_dir, destination)

    result = action_log(dest, lines=lines)
    if not result.get("ok"):
        click.echo(f"Error: {result.get('errors', ['unknown error'])[0]}", err=True)
        sys.exit(1)
    click.echo(result["content"], nl=False)


@main.command()
def encrypt() -> None:
    """Encrypt a secret for use in a config file.

    Prompts (hidden input) for the secret and a password, then prints an
    armored GPG blob. Paste it into a config as e.g. b2_account_id_enc
    or b2_account_key_enc; bu prompts for the password when it is needed.
    """
    import getpass

    from bu.crypto import CryptoError, encrypt_secret

    secret = getpass.getpass("Secret to encrypt: ")
    if not secret:
        click.echo("Error: empty secret", err=True)
        sys.exit(1)
    password = getpass.getpass("Encryption password: ")
    if not password:
        click.echo("Error: empty password", err=True)
        sys.exit(1)

    try:
        blob = encrypt_secret(secret, password)
    except CryptoError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    click.echo(blob)


@main.command()
@_CONFIG_OPTIONAL
@click.argument("name", metavar="[NAME]", required=False, default=None)
def create(config_dir: Path | None, name: str | None) -> None:
    """Interactively create a new destination configuration.

    A guided wizard prompts for the destination name (unless NAME is
    given), backup type (DIR or B2), destination (local directory, or
    B2 bucket/path and credentials), method (rsync/duplicity for DIR,
    duplicity-only for B2), source paths, and method-specific options
    (GPG passphrase).  Invalid answers are rejected and re-prompted;
    choices use numbered, arrow-key menus.
    """
    from bu.wizard import run_create_wizard

    try:
        result = run_create_wizard(config_dir, name=name)
    except (EOFError, KeyboardInterrupt):
        click.echo("\nAborted.")
        sys.exit(1)

    click.echo()
    click.echo(f"Created: {result['file']}")
    d = result["destination"]
    click.echo(f"Destination : {d['name']}")
    click.echo(f"Method      : {d['method']}")
    click.echo(f"Path        : {d['path']}")
    click.echo("Source paths:")
    for sp in d["source_paths"]:
        click.echo(f"  {sp}")
