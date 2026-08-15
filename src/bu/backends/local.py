"""rsync method — copies source directories into a destination subdirectory.

Uses the ``rsync`` binary with sensible flags (archive mode, delete
extraneous files, human-readable stats).  Each source directory is
mirrored directly into the destination directory, and a
``bu-<destination>-status.txt`` file records backup state.

Configuration keys required in the .toml file:
    destination: str — base target directory for backups
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, ClassVar

from bu.backends.base import Backend, LiveWindow, run_streaming


class RsyncMethod(Backend):
    """Mirror source directories directly into the destination directory.

    Each source ``src`` is copied to ``<destination>/<src.name>`` via
    ``rsync -a --delete`` so the destination is an exact mirror of the
    sources.  Maintains a ``bu-<name>-status.txt`` file in the destination
    directory.
    """

    # Default rsync flags used for every backup run.
    RSYNC_FLAGS: ClassVar[list[str]] = [
        "-a",         # archive mode: preserve permissions, times, symlinks
        "--delete",   # remove files in dest that are not in source
        "-h",         # human-readable sizes in output
        "--stats",    # print a statistics summary at the end
    ]

    def _dest_dir(self) -> Path:
        """Return the destination directory (sources are mirrored directly into it)."""
        base = self.config.get("destination", "")
        if not base:
            raise ValueError("rsync method requires 'destination' in config")
        return Path(base).expanduser().resolve()

    def _status_path(self) -> Path:
        """Return path to ``bu-<name>-status.txt`` inside the destination."""
        name = self.config.get("_name", "unknown")
        return self._dest_dir() / f"bu-{name}-status.txt"

    def _write_status(self, state: str, **extra: Any) -> None:
        """Write (or overwrite) bu-<name>-status.txt with the current backup state."""
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
        scroll_lines: int = 0,
    ) -> dict[str, Any]:
        """Run rsync for each source directory into the destination directory.

        Output streams live; with ``scroll_lines`` > 0 it is confined to a
        fixed-height terminal window.
        """
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

        # Exclusion files that actually exist (missing ones are ignored).
        exclude_from = self._existing_exclude_files()

        # One live window shared across all source runs
        window = LiveWindow(
            scroll_lines,
            title=f"Backup output — {self.config.get('_name', '?')}",
        )
        window.__enter__()
        try:
            for src_str in source_paths:
                src = Path(src_str).expanduser().resolve()
                result = self._rsync_one(
                    src, dest / src.name, dry_run, window=window, exclude_from=exclude_from,
                )
                total_files += result["files"]
                total_bytes += result["bytes"]
                all_errors.extend(result["errors"])
                all_stdout.append(result.get("stdout", ""))
                all_stderr.append(result.get("stderr", ""))
        finally:
            window.__exit__(None, None, None)

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
        scroll_lines: int = 0,
    ) -> dict[str, Any]:
        """Restore files from the backup to ``restore_path`` via rsync.

        With ``path_within_backup`` (e.g. ``x/y``), files are restored
        into ``RESTORE_DIR/<final component>`` (i.e. ``RESTORE_DIR/y``).
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

        # Build source: <dest>/[path_within_backup]
        src = self._dest_dir()
        target = restore_dir
        if path_within_backup:
            src = src / path_within_backup
            # Restore into a subdirectory named after the final path
            # component, e.g. path 'x/y' restores into RESTORE_DIR/y.
            target = restore_dir / Path(path_within_backup).name

        if not src.exists():
            return {"files_restored": 0, "bytes_restored": 0,
                    "errors": [f"Backup path not found: {src}"]}

        result = self._rsync_one(
            src,
            target,
            dry_run,
            delete=False,
            scroll_lines=scroll_lines,
            title=f"Restore output — {self.config.get('_name', '?')}",
            exclude=self._status_path().name,
        )

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

    def _existing_exclude_files(self) -> list[str]:
        """Return exclusion-file paths that exist on disk (for --exclude-from)."""
        return [
            p for p in self.config.get("_exclude_files", [])
            if isinstance(p, str) and Path(p).is_file()
        ]

    def _rsync_one(
        self,
        src: Path,
        dest: Path,
        dry_run: bool,
        delete: bool = True,
        scroll_lines: int = 0,
        window: LiveWindow | None = None,
        title: str = "Live output",
        exclude: str | None = None,
        exclude_from: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run rsync for a single source directory → dest subdir.

        ``delete=False`` keeps the restore non-destructive; ``exclude``
        skips a file/directory name (used to keep the bu status file out
        of restores); ``exclude_from`` lists exclusion files to pass via
        ``--exclude-from``.
        Output streams live (``-v`` file listing; ``--progress`` bars when
        stdout is a TTY), confined to a window when ``scroll_lines`` > 0.
        Returns ``{files, bytes, errors, stdout, stderr}``.
        """
        # Ensure dest parent exists (rsync can create the leaf)
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)

        # Trailing slash on src: copy *contents*, not the directory itself.
        cmd = ["rsync", "-a", "-h", "--stats", "-v"]
        if sys.stdout.isatty():
            cmd.append("--progress")
        if delete:
            cmd.append("--delete")
        if exclude:
            cmd.append(f"--exclude={exclude}")
        for ef in exclude_from or []:
            cmd.append(f"--exclude-from={ef}")
        if dry_run:
            cmd.append("--dry-run")
        cmd.append(f"{src}/")
        cmd.append(str(dest))

        try:
            proc = run_streaming(cmd, scroll_lines=scroll_lines, window=window, title=title)
        except FileNotFoundError:
            return {"files": 0, "bytes": 0, "errors": ["rsync binary not found. Is it installed?"],
                    "stdout": "", "stderr": ""}

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

        Checks the config, destination directory, and bu-<name>-status.txt.
        """
        dest_dir = self._dest_dir()
        name = self.config.get("_name", "unknown")
        source_paths = self.config.get("_source_paths", [])

        result: dict[str, Any] = {
            "name": name,
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

        # Read bu-<name>-status.txt if it exists
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
