"""Action handlers for bu — backup, restore, check, verify, status.

Each action receives a DestinationConfig and optional extra args, then delegates
to the appropriate backend implementation.
"""

from __future__ import annotations

import json
from typing import Any

from bu.backends import get_backend
from bu.config import DestinationConfig


def _build_backend(dest: DestinationConfig) -> Any:
    """Instantiate the backend for the given destination config."""
    backend_cls = get_backend(dest.backend)
    return backend_cls(dest.extra)


def action_backup(
    dest: DestinationConfig,
    *,
    dry_run: bool = False,
    extra_args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a backup to the configured destination."""
    backend = _build_backend(dest)
    return backend.backup(dest.source_paths, dry_run=dry_run, extra_args=extra_args)


def action_restore(
    dest: DestinationConfig,
    restore_path: str,
    *,
    dry_run: bool = False,
    extra_args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Restore data from the configured destination."""
    backend = _build_backend(dest)
    return backend.restore(restore_path, dry_run=dry_run, extra_args=extra_args)


def action_check(
    dest: DestinationConfig,
    *,
    extra_args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Check what would be backed up."""
    backend = _build_backend(dest)
    return backend.check(dest.source_paths, extra_args=extra_args)


def action_verify(
    dest: DestinationConfig,
    *,
    full: bool = False,
    extra_args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify integrity of backed-up data."""
    backend = _build_backend(dest)
    return backend.verify(dest.source_paths, full=full, extra_args=extra_args)


def action_status(
    dest: DestinationConfig,
    *,
    extra_args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Show status of the backup destination."""
    backend = _build_backend(dest)
    return backend.status(extra_args=extra_args)


def format_result(result: dict[str, Any], json_output: bool = False) -> str:
    """Format an action result dict for display."""
    if json_output:
        return json.dumps(result, indent=2, default=str)

    lines: list[str] = []
    for key, value in result.items():
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
