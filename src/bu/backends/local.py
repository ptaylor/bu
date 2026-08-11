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
import shutil
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
            p = Path(sp).expanduser().resolve()
            if not p.exists():
                errors.append(f"Source not found: {p}")
            elif not p.is_dir():
                errors.append(f"Source must be a directory (not a file): {p}")
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

        dest = self._dest_dir()

        if not dry_run:
            self._write_status("started")

        total_files = 0
        total_bytes = 0
        all_errors: list[str] = []

        for src_str in source_paths:
            src = Path(src_str).expanduser().resolve()
            result = self._rsync_one(src, dest / src.name, dry_run)
            total_files += result["files"]
            total_bytes += result["bytes"]
            all_errors.extend(result["errors"])

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
        }

    def _rsync_one(self, src: Path, dest: Path, dry_run: bool) -> dict[str, Any]:
        """Run rsync for a single source directory → dest subdir.

        Returns ``{files, bytes, errors}``.
        """
        # Ensure dest parent exists (rsync can create the leaf)
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)

        # Trailing slash on src: copy *contents*, not the directory itself.
        cmd = ["rsync"] + self.RSYNC_FLAGS
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

        return {"files": files, "bytes": bytes_transferred, "errors": errors}

    @staticmethod
    def _parse_rsync_stat(output: str, pattern: str) -> str:
        """Extract the first capture group from an rsync --stats line."""
        m = re.search(pattern, output, re.IGNORECASE)
        return m.group(1).replace(",", "") if m else ""

    @staticmethod
    def _parse_human_size(raw: str) -> int:
        """Parse a human-readable size string like '1.23K' or '4.5M' into bytes."""
        raw = raw.strip().upper().replace(",", "")
        if not raw:
            return 0
        try:
            return int(float(raw))
        except ValueError:
            pass

        multipliers = {"B": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
        for suffix, mult in multipliers.items():
            if raw.endswith(suffix):
                try:
                    return int(float(raw[:-1]) * mult)
                except ValueError:
                    return 0
        # Plain number (no suffix) — treat as bytes
        try:
            return int(float(raw))
        except ValueError:
            return 0

    # ------------------------------------------------------------------
    # remaining actions — unchanged for now
    # ------------------------------------------------------------------

    def restore(
        self,
        restore_path: str,
        *,
        dry_run: bool = False,
        extra_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        dest = self._dest_dir()
        restore = Path(restore_path).expanduser().resolve()

        if not dest.exists():
            return {"files_restored": 0, "bytes_restored": 0, "errors": [f"Backup source not found: {dest}"]}

        result = self._copy_tree(dest, restore, dry_run)
        return {
            "files_restored": result["copied"],
            "files_skipped": result["skipped"],
            "bytes_restored": result["bytes"],
            "errors": result["errors"],
        }

    def check(
        self,
        source_paths: list[str],
        *,
        extra_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        dest = self._dest_dir()
        to_backup: list[str] = []
        to_update: list[str] = []
        total_size = 0

        for src_str in source_paths:
            src = Path(src_str).expanduser().resolve()
            if not src.exists() or not src.is_dir():
                continue

            for root, _, files in os.walk(src):
                for fname in files:
                    sf = Path(root) / fname
                    rel = sf.relative_to(src)
                    df = dest / src.name / rel
                    need, size = self._needs_copy(sf, df)
                    if need:
                        to_backup.append(str(sf))
                        total_size += size
                    elif df.exists():
                        to_update.append(str(sf))

        return {
            "files_to_backup": len(to_backup),
            "files_to_update": len(to_update),
            "total_size": total_size,
            "files": to_backup,
        }

    def verify(
        self,
        source_paths: list[str],
        *,
        full: bool = False,
        extra_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        dest = self._dest_dir()
        verified = 0
        missing: list[str] = []
        mismatched: list[str] = []
        errors: list[str] = []

        for src_str in source_paths:
            src = Path(src_str).expanduser().resolve()
            if not src.exists():
                missing.append(str(src))
                continue

            if src.is_dir():
                for root, _, files in os.walk(src):
                    for fname in files:
                        sf = Path(root) / fname
                        rel = sf.relative_to(src)
                        df = dest / src.name / rel
                        if not df.exists():
                            missing.append(str(sf))
                        elif full:
                            import filecmp
                            if not filecmp.cmp(sf, df, shallow=False):
                                mismatched.append(str(sf))
                            else:
                                verified += 1
                        else:
                            verified += 1

        return {
            "verified": verified,
            "missing": missing,
            "mismatched": mismatched,
            "errors": errors,
        }

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

    # --- helpers ---

    @staticmethod
    def _needs_copy(src: Path, dest: Path) -> tuple[bool, int]:
        """Return (needs_copy, size_bytes) for a single file."""
        size = src.stat().st_size
        if not dest.exists():
            return True, size
        if src.stat().st_mtime > dest.stat().st_mtime:
            return True, size
        if src.stat().st_size != dest.stat().st_size:
            return True, size
        return False, size

    @staticmethod
    def _copy_file(src: Path, dest: Path, dry_run: bool) -> dict[str, Any]:
        """Copy a single file. Returns stats dict."""
        need, size = RsyncMethod._needs_copy(src, dest)
        if not need:
            return {"copied": 0, "skipped": 1, "bytes": 0, "errors": []}

        if not dry_run:
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
            except OSError as e:
                return {"copied": 0, "skipped": 0, "bytes": 0, "errors": [str(e)]}

        return {"copied": 1, "skipped": 0, "bytes": size, "errors": []}

    @staticmethod
    def _copy_tree(src: Path, dest: Path, dry_run: bool) -> dict[str, Any]:
        """Copy a directory tree. Returns stats dict."""
        copied = 0
        skipped = 0
        total_bytes = 0
        errors: list[str] = []

        for root, _, files in os.walk(src):
            for fname in files:
                sf = Path(root) / fname
                rel = sf.relative_to(src)
                df = dest / rel
                result = RsyncMethod._copy_file(sf, df, dry_run)
                copied += result["copied"]
                skipped += result["skipped"]
                total_bytes += result["bytes"]
                errors.extend(result["errors"])

        return {"copied": copied, "skipped": skipped, "bytes": total_bytes, "errors": errors}
