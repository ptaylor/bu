"""CLI entry point for bu — the backup utility.

Usage:
    bu ACTION [ARGS...]

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
    help      Show usage for bu or for a specific command
"""

from __future__ import annotations

import sys
from typing import Any

import click

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


# Shared option/argument builders
_NAME_ARG = click.argument("name", metavar="NAME")


def _command(name: str | None = None, **kwargs: Any) -> Any:
    """Register a command on main without the '[OPTIONS]' usage placeholder."""
    kwargs.setdefault("options_metavar", "")
    return main.command(name, **kwargs)


def _load_config() -> Config:
    """Load configuration, printing a friendly error and exiting on failure."""
    try:
        return Config()
    except ConfigError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


def _resolve_destination(name: str) -> DestinationConfig:
    """Load config and resolve the named destination, with a friendly error."""
    cfg = _load_config()
    try:
        return cfg.get(name)
    except ConfigError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


def _default_window() -> int:
    """Live-output window height: 18 lines on a TTY, 0 (plain text) when piped."""
    return 18 if sys.stdout.isatty() else 0


@click.group(
    cls=OrderedGroup,
    invoke_without_command=True,
    context_settings={"help_option_names": []},
    options_metavar="",
)
@click.pass_context
def main(ctx: click.Context) -> None:
    """bu — Backup utility for managing backups via rsync or duplicity.

    Run 'bu help ACTION' for details on each action.
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
                click.echo("\nNo destinations configured. Run 'bu create' to configure one.")
        except ConfigError:
            click.echo("\nNo config directory found. Run 'bu create' to get started.")


@_command()
@_NAME_ARG
def backup(name: str) -> None:
    """Back up source paths to NAME."""
    dest_cfg = _resolve_destination(name)

    result = action_backup(dest_cfg, scroll_lines=_default_window())
    click.echo(format_result(result))

    # Exit non-zero if there were errors
    if result.get("errors"):
        sys.exit(1)


@_command()
@_NAME_ARG
@click.argument("restore_dir", metavar="RESTORE_DIR")
@click.argument("path_within_backup", metavar="[PATH]", required=False, default=None)
def restore(
    name: str,
    restore_dir: str,
    path_within_backup: str | None,
) -> None:
    """Restore files from NAME into RESTORE_DIR.

    RESTORE_DIR must exist. PATH optionally narrows the restore to a
    subpath within the backup; its files are restored into
    RESTORE_DIR/<final component> (e.g. path 'x/y' restores into
    RESTORE_DIR/y). Restore never deletes files.
    """
    dest_cfg = _resolve_destination(name)

    result = action_restore(
        dest_cfg,
        restore_dir,
        path_within_backup,
        scroll_lines=_default_window(),
    )
    click.echo(format_result(result))

    if result.get("errors"):
        sys.exit(1)


@_command()
@_NAME_ARG
def status(name: str) -> None:
    """Show status of the backup for NAME."""
    dest_cfg = _resolve_destination(name)

    result = action_status(dest_cfg)
    click.echo(format_status(result))


@_command("list")
def list_destinations() -> None:
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

    if not rows:
        click.echo("No destinations configured.")
        click.echo("Run 'bu create' to configure one.")
        return

    for r in rows:
        if r.get("error"):
            click.echo(f"{r['name']:<24} <invalid config: {r['error']}>")
        else:
            click.echo(f"{r['name']:<24} {r['method']:<10} {r['destination']}")


@_command()
@click.argument("name", metavar="[NAME]", required=False, default=None)
def config(name: str | None) -> None:
    """Edit a destination configuration in $EDITOR.

    If NAME is given, opens (or creates) its .toml file.
    Without NAME, lists all configured destinations.
    """
    result = action_config(None, name=name)
    click.echo(format_result(result))

    if not result.get("ok"):
        sys.exit(1)


@_command()
@_NAME_ARG
def delete(name: str) -> None:
    """Delete the NAME config file.

    Only the .toml config is removed — the backup contents at the
    destination are NOT deleted.
    """
    cfg = _load_config()

    if not VALID_NAME_RE.match(name):
        click.echo(
            f"Error: Invalid name {name!r} — names may only "
            "contain letters, digits, '-' and '_'.",
            err=True,
        )
        sys.exit(1)

    file_path = cfg.config_dir / f"{name}.toml"
    if not file_path.exists():
        available = ", ".join(cfg.list_destinations()) or "(none)"
        click.echo(
            f"Error: No config found for {name!r}. "
            f"Available destinations: {available}",
            err=True,
        )
        sys.exit(1)

    # Best-effort: find where the backup contents live for the warning.
    dest_path: str | None = None
    try:
        dest_path = cfg.get(name).destination
    except ConfigError:
        pass

    click.echo("Warning: this deletes only the config file — the backup contents are NOT deleted.")
    try:
        confirmed = click.confirm(f"Delete config for {name!r}?", default=False)
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


@_command()
@_NAME_ARG
def history(name: str) -> None:
    """Show the action history for NAME.

    Displays backup and restore actions in reverse chronological order
    (latest first). Each action shows start/end timestamps, file/size
    stats, and any errors or notes recorded during the run.
    """
    dest_cfg = _resolve_destination(name)

    result = action_history(dest_cfg)
    click.echo(format_history(result))


@_command()
@_NAME_ARG
def log(name: str) -> None:
    """Print the raw execution log for NAME to stdout."""
    dest_cfg = _resolve_destination(name)

    result = action_log(dest_cfg)
    if not result.get("ok"):
        click.echo(f"Error: {result.get('errors', ['unknown error'])[0]}", err=True)
        sys.exit(1)
    click.echo(result["content"], nl=False)


@_command()
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


@_command()
@click.argument("name", metavar="[NAME]", required=False, default=None)
def create(name: str | None) -> None:
    """Interactively create a new destination configuration.

    A guided wizard prompts for the destination name (unless NAME is
    given), backup type (DIR or B2), destination (local directory, or
    B2 bucket/path and credentials), method (rsync/duplicity for DIR,
    duplicity-only for B2), source paths, and method-specific options
    (GPG passphrase).  It also sets up global and per-name exclusion
    files.  Invalid answers are rejected and re-prompted; choices use
    numbered, arrow-key menus.
    """
    from bu.wizard import run_create_wizard

    try:
        result = run_create_wizard(None, name=name)
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


@_command()
@click.argument("command", metavar="[COMMAND]", required=False, default=None)
def help_command(command: str | None) -> None:
    """Show usage for bu, or for a specific command."""
    ctx = click.get_current_context()
    if command:
        cmd = main.get_command(ctx, command)
        if cmd is None:
            click.echo(f"Error: Unknown command {command!r}.", err=True)
            sys.exit(1)
        with click.Context(cmd, info_name=command, parent=ctx.parent) as sub_ctx:
            click.echo(cmd.get_help(sub_ctx))
    else:
        click.echo(main.get_help(ctx.parent))
