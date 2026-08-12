"""rsync method — copies source directories into a destination subdirectory.

Uses the ``rsync`` binary with sensible flags (archive mode, delete
extraneous files, human-readable stats).  A ``bu-status.json`` file is
written into the destination subdirectory to track backup state.

Configuration keys required in the .toml file:
    destination: str — base target directory for backups
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from bu.backends.base import Backend


class RsyncMethod(Backend):
    """Copy source directories to ``<destination>/<name>/`` via rsync.

    Uses ``rsync -a --delete`` so the destination is an exact mirror of
    the sources.  Maintains a ``bu-status.json`` file in the destination
    subdirectory.
    """

    # Default rsync flags used for every backup run.
    RSYNC_FLAGS = [
        "-a",         # archive mode: preserve permissions, times, symlinks
        "--delete",   # remove files in dest that are not in source
        "-h",         # human-readable sizes in output
        "--stats",    # print a statistics summary at the end
    ]

    def _dest_dir(self) -> Path:
        """Return ``<destination>/<name>/``."""
        base = self.config.get("destination", "")
        if not base:
            raise ValueError("rsync method requires 'destination' in config")
        name = self.config.get("_name", "unknown")
        return Path(base).expanduser().resolve() / name

    def _status_path(self) -> Path:
        """Return path to ``bu-status.json`` inside the destination."""
        return self._dest_dir() / "bu-status.json"

    def _write_status(self, state: str, **extra: Any) -> None:
        """Write (or overwrite) bu-status.json with the current backup state."""
        status: dict[str, Any] = {
            "destination": self.config.get("_name", "unknown"),
            "method": "rsync",
            "source_paths": self.config.get("_source_paths", []),
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "state": state,
        }
        status.update(extra)

        dest_dir = self._dest_dir()
        dest_dir.mkdir(parents=True, exist_ok=True)
        with open(self._status_path(), "w") as fh:
            json.dump(status, fh, indent=2, default=str)

    def _validate_sources_are_dirs(self, source_paths: list[str]) -> list[str]:
        """Check that every source path is a directory.  Returns error list."""
        errors: list[str] = []
        for sp in source_paths:
            display = Path(sp).expanduser()
            real = display.resolve()
            if not real.exists():
                errors.append(f"Source not found: {display}")
            elif not real.is_dir():
                errors.append(f"Source must be a directory (not a file): {display}")
        return errors

    def _validate_destination(self) -> list[str]:
        """Check that the destination base path exists and is writable."""
        errors: list[str] = []
        base = Path(self.config.get("destination", "")).expanduser()
        if not base.exists():
            errors.append(f"Destination not found: {base}")
        elif not base.is_dir():
            errors.append(f"Destination is not a directory: {base}")
        elif not os.access(base, os.W_OK):
            errors.append(f"Destination not writable: {base}")
        return errors

    # ------------------------------------------------------------------
    # backup action (the only action using real rsync for now)
    # ------------------------------------------------------------------

    def backup(
        self,
        source_paths: list[str],
        *,
        dry_run: bool = False,
        extra_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run rsync for each source directory into the destination subdir."""
        errors = self._validate_sources_are_dirs(source_paths)
        if errors:
            return {"files_copied": 0, "files_skipped": 0, "bytes_copied": 0, "errors": errors}

        # Validate destination base path is usable
        dest_errors = self._validate_destination()
        if dest_errors:
            return {"files_copied": 0, "files_skipped": 0, "bytes_copied": 0, "errors": dest_errors}

        dest = self._dest_dir()

        if not dry_run:
            self._write_status("started")

        total_files = 0
        total_bytes = 0
        all_errors: list[str] = []
        all_stdout: list[str] = []
        all_stderr: list[str] = []

        for src_str in source_paths:
            src = Path(src_str).expanduser().resolve()
            result = self._rsync_one(src, dest / src.name, dry_run)
            total_files += result["files"]
            total_bytes += result["bytes"]
            all_errors.extend(result["errors"])
            all_stdout.append(result.get("stdout", ""))
            all_stderr.append(result.get("stderr", ""))

        if not dry_run:
            if all_errors:
                self._write_status(
                    "error",
                    files_copied=total_files,
                    bytes_copied=total_bytes,
                    errors=all_errors,
                )
            else:
                self._write_status(
                    "completed",
                    files_copied=total_files,
                    bytes_copied=total_bytes,
                )

        return {
            "files_copied": total_files,
            "files_skipped": 0,
            "bytes_copied": total_bytes,
            "errors": all_errors,
            "stdout": "\n".join(all_stdout).strip(),
            "stderr": "\n".join(all_stderr).strip(),
            "rsync_version": self._rsync_version(),
        }

    # ------------------------------------------------------------------
    # restore action
    # ------------------------------------------------------------------

    def restore(
        self,
        restore_path: str,
        path_within_backup: str | None = None,
        *,
        dry_run: bool = False,
        extra_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Restore files from the backup to ``restore_path`` via rsync.

        Never deletes files in the restore destination (no --delete).
        """
        # Validate restore destination
        errors: list[str] = []
        restore_dir = Path(restore_path).expanduser()
        if not restore_dir.exists():
            errors.append(f"Restore destination not found: {restore_dir}")
        elif not restore_dir.is_dir():
            errors.append(f"Restore destination is not a directory: {restore_dir}")
        if errors:
            return {"files_restored": 0, "bytes_restored": 0, "errors": errors}

        # Build source: <dest>/<name>/[path_within_backup]
        src = self._dest_dir()
        if path_within_backup:
            src = src / path_within_backup

        if not src.exists():
            return {"files_restored": 0, "bytes_restored": 0,
                    "errors": [f"Backup path not found: {src}"]}

        result = self._rsync_one(src, restore_dir, dry_run, delete=False)

        return {
            "files_restored": result["files"],
            "bytes_restored": result["bytes"],
            "errors": result["errors"],
            "stdout": result.get("stdout", ""),
            "stderr": result.get("stderr", ""),
            "rsync_version": self._rsync_version(),
        }

    def _rsync_version(self) -> str:
        """Return the rsync version string, or empty on failure."""
        try:
            proc = subprocess.run(
                ["rsync", "--version"], capture_output=True, text=True, check=False,
            )
            # First line is typically "rsync  version 3.x.x  ..."
            return proc.stdout.split("\n")[0].strip()
        except (FileNotFoundError, OSError):
            return ""

    def _rsync_one(
        self,
        src: Path,
        dest: Path,
        dry_run: bool,
        delete: bool = True,
    ) -> dict[str, Any]:
        """Run rsync for a single source directory → dest subdir.

        ``delete=False`` keeps the restore non-destructive.
        Returns ``{files, bytes, errors, stdout, stderr}``.
        """
        # Ensure dest parent exists (rsync can create the leaf)
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)

        # Trailing slash on src: copy *contents*, not the directory itself.
        cmd = ["rsync", "-a", "-h", "--stats"]
        if delete:
            cmd.append("--delete")
        if dry_run:
            cmd.append("--dry-run")
        cmd.append(f"{src}/")
        cmd.append(str(dest))

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except FileNotFoundError:
            return {"files": 0, "bytes": 0, "errors": ["rsync binary not found. Is it installed?"]}

        errors: list[str] = []
        if proc.returncode != 0:
            # rsync exit codes: 0=success, >0=partial or error
            stderr = proc.stderr.strip()
            if stderr:
                errors.append(stderr)

        # Parse --stats summary for file count and byte total
        files_str = self._parse_rsync_stat(proc.stdout, r"Number of (?:regular )?files(?: transferred)?:\s+([\d,]+)")
        try:
            files = int(files_str) if files_str else 0
        except ValueError:
            files = 0

        # "total size is N" line gives bytes as a plain integer
        size_str = self._parse_rsync_stat(proc.stdout, r"total size is ([\d,]+)")
        try:
            bytes_transferred = int(size_str) if size_str else 0
        except ValueError:
            bytes_transferred = 0

        return {"files": files, "bytes": bytes_transferred, "errors": errors,
                "stdout": proc.stdout, "stderr": proc.stderr}

    @staticmethod
    def _parse_rsync_stat(output: str, pattern: str) -> str:
        """Extract the first capture group from an rsync --stats line."""
        m = re.search(pattern, output, re.IGNORECASE)
        return m.group(1).replace(",", "") if m else ""

    def status(
        self,
        *,
        extra_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return comprehensive status about this backup destination.

        Checks the config, destination directory, and bu-status.json.
        """
        dest_dir = self._dest_dir()
        name = self.config.get("_name", "unknown")
        source_paths = self.config.get("_source_paths", [])

        result: dict[str, Any] = {
            "destination": name,
            "method": "rsync",
            "config_ok": True,
            "source_paths": source_paths,
            "dest_path": str(dest_dir),
            "dest_exists": dest_dir.is_dir(),
        }

        # Check existence of each source path
        sources_status: list[dict[str, Any]] = []
        for sp in source_paths:
            p = Path(sp).expanduser()
            sources_status.append({
                "path": str(p),
                "exists": p.is_dir(),
            })
        result["sources"] = sources_status

        # Read bu-status.json if it exists
        sp = self._status_path()
        if sp.exists():
            try:
                with open(sp) as fh:
                    status_data = json.load(fh)
                result["last_backup"] = status_data
            except (json.JSONDecodeError, OSError) as e:
                result["status_file_error"] = str(e)
        else:
            result["last_backup"] = None

        return result

    # --- helpers (only _parse_rsync_stat retained for backup) ---
