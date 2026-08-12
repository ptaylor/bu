"""Action handlers for bu — backup, status, config, history.

Each action receives a DestinationConfig and optional extra args, then delegates
to the appropriate method implementation.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from bu.backends import get_backend
from bu.config import Config, ConfigError, DestinationConfig
from bu.logging import ActionLogger, group_entries, read_log

# Per-method sample configs used when creating a new destination file.
_SAMPLE_RSYNC = """\
# rsync method — copies files to a local directory
method = "rsync"
source_paths = ["~/Documents", "~/notes"]
destination = "/mnt/backup/docs"
{history_file}
{log_file}
"""

_SAMPLE_DUPLICITY = """\
# duplicity method — encrypted, incremental backup archives
method = "duplicity"
source_paths = ["~/Documents", "~/notes"]
destination = "/mnt/backup/docs"
{history_file}
{log_file}
"""

_SAMPLE_GENERIC = """\
# bu destination configuration
# method = "rsync"   # or "duplicity"
# source_paths = ["/path/to/backup"]
# destination = "/path/to/backup/location"
{history_file}
{log_file}
"""


def _build_backend(dest: DestinationConfig) -> Any:
    """Instantiate the backend for the given destination config."""
    backend_cls = get_backend(dest.method)
    # Pass all necessary info to the backend via config
    config = dict(dest.extra)
    config["destination"] = dest.destination
    config["_name"] = dest.name
    config["_source_paths"] = list(dest.source_paths)
    return backend_cls(config)


def action_backup(
    dest: DestinationConfig,
    *,
    dry_run: bool = False,
    extra_args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a backup to the configured destination."""
    start_ts = datetime.datetime.now(datetime.timezone.utc)

    logger = ActionLogger(dest.history_file)
    logger.start("backup", dest.name)

    backend = _build_backend(dest)
    result = backend.backup(dest.source_paths, dry_run=dry_run, extra_args=extra_args)

    # Log any errors from the backend result
    for err in result.get("errors", []):
        logger.error(str(err))

    logger.end(
        files_copied=result.get("files_copied", 0),
        files_skipped=result.get("files_skipped", 0),
        bytes_copied=result.get("bytes_copied", 0),
        dry_run=dry_run,
    )

    # Write raw execution log
    _write_raw_log(dest, "backup", start_ts, result)

    return result


def _write_raw_log(
    dest: DestinationConfig,
    action: str,
    start_ts: datetime.datetime,
    result: dict[str, Any],
    restore_to: str | None = None,
) -> None:
    """Append a raw execution log entry to the destination's log file."""
    end_ts = datetime.datetime.now(datetime.timezone.utc)
    log_path = dest.log_file
    log_path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    lines.append(f"{'='*60}")
    lines.append(f"Action      : {action}")
    lines.append(f"Destination : {dest.name}")
    lines.append(f"Method      : {dest.method}")
    lines.append(f"Started     : {start_ts.isoformat()}")
    lines.append(f"Ended       : {end_ts.isoformat()}")
    if restore_to:
        lines.append(f"Restore to  : {restore_to}")
    else:
        lines.append(f"Source paths:")
        for sp in dest.source_paths:
            lines.append(f"  {sp}")
        lines.append(f"Dest path   : {dest.destination}")
    lines.append(f"Dry run     : {result.get('dry_run', False)}")

    # Rsync version (only present for rsync method)
    version = result.get("rsync_version", "")
    if version:
        lines.append(f"Rsync ver   : {version}")

    # Stats
    for label, key in [("Files copied", "files_copied"), ("Files restored", "files_restored"),
                        ("Bytes copied", "bytes_copied"), ("Bytes restored", "bytes_restored")]:
        if key in result:
            lines.append(f"{label:12s} {result[key]}")

    errors = result.get("errors", [])
    if errors:
        lines.append(f"Errors      : {len(errors)}")
        for err in errors:
            lines.append(f"  {err}")

    # Raw stdout from the backend
    stdout = result.get("stdout", "")
    if stdout:
        lines.append(f"{'-'*40}")
        lines.append("--- command output ---")
        lines.append(stdout)

    stderr = result.get("stderr", "")
    if stderr:
        lines.append(f"{'-'*40}")
        lines.append("--- stderr ---")
        lines.append(stderr)

    lines.append("")

    with open(log_path, "a") as fh:
        fh.write("\n".join(lines) + "\n")


def action_restore(
    dest: DestinationConfig,
    restore_path: str,
    path_within_backup: str | None = None,
    *,
    dry_run: bool = False,
    extra_args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Restore files from the configured destination."""
    start_ts = datetime.datetime.now(datetime.timezone.utc)

    logger = ActionLogger(dest.history_file)
    logger.start("restore", dest.name)

    backend = _build_backend(dest)
    result = backend.restore(
        restore_path,
        path_within_backup,
        dry_run=dry_run,
        extra_args=extra_args,
    )

    for err in result.get("errors", []):
        logger.error(str(err))

    logger.end(
        files_restored=result.get("files_restored", 0),
        bytes_restored=result.get("bytes_restored", 0),
        dry_run=dry_run,
    )

    _write_raw_log(dest, "restore", start_ts, result, restore_to=restore_path)

    return result


def action_status(
    dest: DestinationConfig,
    *,
    extra_args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Show status of the backup destination."""
    backend = _build_backend(dest)
    return backend.status(extra_args=extra_args)


def _default_config_dir() -> Path:
    """Return the default config directory path."""
    if env_dir := os.environ.get("BU_CONFIG_DIR"):
        return Path(env_dir)
    xdg = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return Path(xdg) / "bu"


def _sample_for_method(method: str, destination: str) -> str:
    """Return a sample config template for the given method type.

    Both ``history_file`` and ``log_file`` are pre-filled with their
    default computed paths.
    """
    from bu.config import _default_history_dir, _default_raw_log_dir

    history_path = _default_history_dir() / f"{destination}.json"
    history_line = f'history_file = "{history_path}"'

    log_path = _default_raw_log_dir() / f"{destination}.log"
    log_line = f'log_file = "{log_path}"'

    samples: dict[str, str] = {
        "rsync": _SAMPLE_RSYNC,
        "duplicity": _SAMPLE_DUPLICITY,
    }
    template = samples.get(method, _SAMPLE_GENERIC)
    return template.format(history_file=history_line, log_file=log_line)


def action_config(
    config_dir: Path | None = None,
    *,
    destination: str | None = None,
    method: str | None = None,
) -> dict[str, Any]:
    """Open a destination config file in $EDITOR (default vi) for editing.

    If the config file does not exist, it is created with sample content
    for the given method (or a generic template if no method specified).
    After the editor closes, the TOML is validated.

    Parameters
    ----------
    config_dir : Path or None
        Explicit config directory. If None, uses the default path.
    destination : str or None
        The destination name to edit. Required for editing a specific destination.
        If None, lists all configured destinations.
    method : str or None
        Method type hint for the sample template (only used when creating a new file).
    """
    cfg_dir = config_dir or _default_config_dir()

    # If no destination specified, list what's available
    if not destination:
        cfg_dir.mkdir(parents=True, exist_ok=True)
        cfg = Config(cfg_dir)
        dests = cfg.list_destinations()
        if dests:
            return {
                "ok": True,
                "config_dir": str(cfg_dir),
                "destinations": dests,
                "hint": f"Run 'bu config <name>' to edit a destination, "
                        f"or 'bu config <name> --method rsync|duplicity' to create one.",
            }
        else:
            return {
                "ok": True,
                "config_dir": str(cfg_dir),
                "destinations": [],
                "hint": "No destinations yet. "
                        "Run 'bu config <name> --method rsync|duplicity' to create one.",
            }

    # Determine the file path for this destination
    file_path = cfg_dir / f"{destination}.toml"

    # Create the file with sample content if it doesn't exist
    created = False
    if not file_path.exists():
        cfg_dir.mkdir(parents=True, exist_ok=True)
        file_path.write_text(_sample_for_method(method or "", destination))
        created = True

    # Determine the editor
    editor = os.environ.get("EDITOR", "vi")

    # Open the editor
    try:
        subprocess.run([editor, str(file_path)], check=False)
    except FileNotFoundError:
        return {
            "ok": False,
            "destination": destination,
            "created": created,
            "file": str(file_path),
            "errors": [f"Editor '{editor}' not found. Set $EDITOR or install vi."],
        }

    # Validate the TOML after editing
    try:
        with open(file_path, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        return {
            "ok": False,
            "destination": destination,
            "created": created,
            "file": str(file_path),
            "errors": [f"Invalid TOML: {e}"],
        }
    except OSError as e:
        return {
            "ok": False,
            "destination": destination,
            "created": created,
            "file": str(file_path),
            "errors": [f"Cannot read config: {e}"],
        }

    # Validate the required keys
    if not isinstance(raw, dict):
        return {
            "ok": False,
            "destination": destination,
            "created": created,
            "file": str(file_path),
            "errors": ["Config must contain key-value pairs (not an array)."],
        }

    raw_method = raw.get("method", "")
    if not raw_method:
        return {
            "ok": False,
            "destination": destination,
            "created": created,
            "file": str(file_path),
            "errors": ["Missing required 'method' key. Must be one of: rsync, duplicity."],
        }

    if raw_method not in ("rsync", "duplicity"):
        return {
            "ok": False,
            "destination": destination,
            "created": created,
            "file": str(file_path),
            "errors": [f"Unknown method {raw_method!r}. Must be one of: rsync, duplicity."],
        }

    source_paths = raw.get("source_paths", [])
    if not source_paths:
        return {
            "ok": False,
            "destination": destination,
            "created": created,
            "file": str(file_path),
            "errors": ["Missing required 'source_paths' key."],
        }

    raw_dest = raw.get("destination", "")
    if not raw_dest:
        return {
            "ok": False,
            "destination": destination,
            "created": created,
            "file": str(file_path),
            "errors": ["Missing required 'destination' key (backup target path)."],
        }

    # Build a DestinationConfig directly from the parsed data so we
    # validate *only* this file — not every .toml in the directory.
    dest_cfg = DestinationConfig(destination, raw)

    return {
        "ok": True,
        "destination": {
            "name": destination,
            "method": dest_cfg.method,
            "path": dest_cfg.destination,
            "source_paths": dest_cfg.source_paths,
        },
        "created": created,
        "file": str(file_path),
    }


def action_history(
    dest: DestinationConfig,
    *,
    limit: int = 0,
) -> dict[str, Any]:
    """Return the action history for a destination.

    Reads the JSON-lines log file, groups entries by correlation_id,
    and returns them in reverse chronological order (latest first).

    Parameters
    ----------
    dest : DestinationConfig
        The destination whose history to fetch.
    limit : int
        Maximum number of action groups to return. 0 means no limit.
    """
    entries = read_log(dest.history_file)
    groups = group_entries(entries)

    # Build a summary for each correlation_id group
    summaries: list[dict[str, Any]] = []
    for cid, group in groups.items():
        start_ts: str | None = None
        end_ts: str | None = None
        action = ""
        destination = ""
        errors: list[str] = []
        notes: list[str] = []
        extra: dict[str, Any] = {}

        for entry in group:
            etype = entry.get("type", "")
            action = entry.get("action", action)
            destination = entry.get("destination", destination)
            ts = entry.get("timestamp", "")

            if etype == "start":
                start_ts = ts
            elif etype == "end":
                end_ts = ts
                # Capture stats from the end entry
                for key in ("files_copied", "files_restored", "files_skipped",
                            "bytes_copied", "bytes_restored", "dry_run"):
                    if key in entry:
                        extra[key] = entry[key]
            elif etype == "error":
                errors.append(entry.get("message", ""))
            elif etype == "note":
                notes.append(entry.get("message", ""))

        summaries.append({
            "correlation_id": cid,
            "action": action,
            "destination": destination,
            "start": start_ts,
            "end": end_ts,
            "errors": errors,
            "notes": notes,
            "extra": extra,
        })

    # Sort reverse-chronological by start time
    summaries.sort(key=lambda s: s.get("start", ""), reverse=True)

    if limit > 0:
        summaries = summaries[:limit]

    return {
        "destination": dest.name,
        "history_file": str(dest.history_file),
        "total_actions": len(summaries),
        "history": summaries,
    }


def _format_timestamp(ts: str | None) -> str:
    """Format an ISO timestamp for display."""
    if not ts:
        return "—"
    try:
        dt = datetime.datetime.fromisoformat(ts)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return ts


def _format_size(size_bytes: int) -> str:
    """Format a byte count into a human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.0f} {unit}" if unit == "B" else f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


def format_history(result: dict[str, Any], json_output: bool = False) -> str:
    """Format history results for display."""
    if json_output:
        return json.dumps(result, indent=2, default=str)

    history = result.get("history", [])
    if not history:
        return f"No action history for '{result.get('destination', '?')}'.\nLog: {result.get('history_file', '?')}"

    lines: list[str] = []
    for h in history:
        action = h.get("action", "?")
        dest = h.get("destination", "?")
        start = _format_timestamp(h.get("start"))
        end = _format_timestamp(h.get("end"))
        extra = h.get("extra", {})

        # Build the one-line summary
        parts = [f"{start}  {action:8s}  {dest}"]
        if h.get("end"):
            parts.append(f"  {start.split()[-1] if start else '?'} → {end.split()[-1] if end else '?'}")

        # Add stats if available
        stats: list[str] = []
        for label, key in [("files", "files_copied"), ("files", "files_restored")]:
            if key in extra:
                stats.append(f"{extra[key]} {label}")
        for label, key in [("size", "bytes_copied"), ("size", "bytes_restored")]:
            if key in extra and extra[key]:
                stats.append(_format_size(extra[key]))
        if extra.get("dry_run"):
            stats.append("dry-run")
        if stats:
            parts.append(f"  ({', '.join(stats)})")

        lines.append("".join(parts))

        # Indented errors and notes
        for err in h.get("errors", []):
            lines.append(f"  ✗ {err}")
        for note in h.get("notes", []):
            lines.append(f"  ℹ {note}")

    return "\n".join(lines)


def format_result(result: dict[str, Any], json_output: bool = False) -> str:
    """Format an action result dict for display."""
    if json_output:
        return json.dumps(result, indent=2, default=str)

    # Internal keys not meant for display
    _skip = {"stdout", "stderr", "rsync_version", "dry_run"}

    lines: list[str] = []
    for key, value in result.items():
        if key in _skip:
            continue
        if key == "errors" and isinstance(value, list) and value:
            lines.append(f"errors: {len(value)} error(s)")
            for err in value:
                lines.append(f"  - {err}")
        elif key == "files" and isinstance(value, list):
            lines.append(f"{key}: {len(value)} file(s)")
        elif isinstance(value, list):
            if value:
                lines.append(f"{key}: {len(value)} item(s)")
        else:
            lines.append(f"{key}: {value}")
    return "\n".join(lines)


def format_status(result: dict[str, Any], json_output: bool = False) -> str:
    """Format status results for display."""
    if json_output:
        return json.dumps(result, indent=2, default=str)

    lines: list[str] = []
    lines.append(f"Destination  : {result.get('destination', '?')}")
    lines.append(f"Method       : {result.get('method', '?')}")

    sources = result.get("sources", [])
    if sources:
        lines.append("Source paths :")
        for s in sources:
            icon = "✓" if s.get("exists") else "✗"
            lines.append(f"  {icon} {s.get('path', '?')}")
    else:
        # Fallback to flat list for JSON or older backends
        source_paths = result.get("source_paths", [])
        if source_paths:
            lines.append("Source paths :")
            for sp in source_paths:
                lines.append(f"  {sp}")

    lines.append(f"Dest path    : {result.get('dest_path', '?')}")
    lines.append(f"Dest exists  : {'yes' if result.get('dest_exists') else 'no'}")

    last = result.get("last_backup")
    if last is None:
        lines.append("")
        lines.append("No backup has been run yet.")
    elif isinstance(last, dict):
        lines.append("")
        lines.append("Last backup")
        lines.append("-----------")
        state = last.get("state", "?")
        state_icon = {"completed": "✓", "started": "…", "error": "✗"}.get(state, "?")
        lines.append(f"  State       : {state_icon} {state}")
        ts = last.get("timestamp", "")
        if ts:
            try:
                dt = datetime.datetime.fromisoformat(ts)
                ts = dt.strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                pass
            lines.append(f"  Timestamp   : {ts}")
        for key, label in [("files_copied", "Files copied"), ("bytes_copied", "Bytes copied")]:
            if key in last:
                val = last[key]
                if label == "Bytes copied" and isinstance(val, (int, float)):
                    val = _format_size(int(val))
                lines.append(f"  {label:12s} {val}")
        errors = last.get("errors", [])
        if errors:
            lines.append(f"  Errors      : {len(errors)}")
            for err in errors:
                lines.append(f"    - {err}")
    elif result.get("status_file_error"):
        lines.append(f"  Status file : corrupt ({result['status_file_error']})")

    return "\n".join(lines)
