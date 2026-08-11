"""Local filesystem backend — copies files to a local directory.

Uses rsync-style logic: compares source and destination files by mtime/size,
and copies only changed or missing files.

Configuration keys:
    target_path: str — destination directory for backups
"""

from __future__ import annotations

import filecmp
import os
import shutil
from pathlib import Path
from typing import Any

from bu.backends.base import Backend


class LocalBackend(Backend):
    """Back up to a local filesystem path."""

    def _validate(self) -> Path:
        target = self.config.get("target_path", "")
        if not target:
            raise ValueError("Local backend requires 'target_path' in config")
        dest = Path(target).expanduser().resolve()
        return dest

    def backup(
        self,
        source_paths: list[str],
        *,
        dry_run: bool = False,
        extra_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        dest = self._validate()
        files_copied = 0
        files_skipped = 0
        bytes_copied = 0
        errors: list[str] = []

        for src_str in source_paths:
            src = Path(src_str).expanduser().resolve()
            if not src.exists():
                errors.append(f"Source not found: {src}")
                continue

            if src.is_file():
                result = self._copy_file(src, dest / src.name, dry_run)
            else:
                result = self._copy_tree(src, dest / src.name, dry_run)

            files_copied += result["copied"]
            files_skipped += result["skipped"]
            bytes_copied += result["bytes"]
            errors.extend(result["errors"])

        return {
            "files_copied": files_copied,
            "files_skipped": files_skipped,
            "bytes_copied": bytes_copied,
            "errors": errors,
        }

    def restore(
        self,
        restore_path: str,
        *,
        dry_run: bool = False,
        extra_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        dest = self._validate()
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
        dest = self._validate()
        to_backup: list[str] = []
        to_update: list[str] = []
        total_size = 0

        for src_str in source_paths:
            src = Path(src_str).expanduser().resolve()
            if not src.exists():
                continue

            if src.is_file():
                need, size = self._needs_copy(src, dest / src.name)
                if need:
                    to_backup.append(str(src))
                    total_size += size
            else:
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
        dest = self._validate()
        verified = 0
        missing: list[str] = []
        mismatched: list[str] = []
        errors: list[str] = []

        for src_str in source_paths:
            src = Path(src_str).expanduser().resolve()
            if not src.exists():
                missing.append(str(src))
                continue

            if src.is_file():
                df = dest / src.name
                if not df.exists():
                    missing.append(str(src))
                elif full and not filecmp.cmp(src, df, shallow=False):
                    mismatched.append(str(src))
                else:
                    verified += 1
            else:
                for root, _, files in os.walk(src):
                    for fname in files:
                        sf = Path(root) / fname
                        rel = sf.relative_to(src)
                        df = dest / src.name / rel
                        if not df.exists():
                            missing.append(str(sf))
                        elif full and not filecmp.cmp(sf, df, shallow=False):
                            mismatched.append(str(sf))
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
        dest = self._validate()
        if not dest.exists():
            return {
                "exists": False,
                "file_count": 0,
                "total_size": 0,
                "last_backup": None,
            }

        file_count = 0
        total_size = 0
        latest_mtime = 0.0

        for root, _, files in os.walk(dest):
            for fname in files:
                fp = Path(root) / fname
                file_count += 1
                total_size += fp.stat().st_size
                mtime = fp.stat().st_mtime
                if mtime > latest_mtime:
                    latest_mtime = mtime

        import datetime
        last_backup = (
            datetime.datetime.fromtimestamp(latest_mtime).isoformat()
            if latest_mtime > 0
            else None
        )

        return {
            "exists": True,
            "file_count": file_count,
            "total_size": total_size,
            "last_backup": last_backup,
        }

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
    def _copy_file(
        src: Path, dest: Path, dry_run: bool
    ) -> dict[str, Any]:
        """Copy a single file. Returns stats dict."""
        need, size = LocalBackend._needs_copy(src, dest)
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
    def _copy_tree(
        src: Path, dest: Path, dry_run: bool
    ) -> dict[str, Any]:
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
                result = LocalBackend._copy_file(sf, df, dry_run)
                copied += result["copied"]
                skipped += result["skipped"]
                total_bytes += result["bytes"]
                errors.extend(result["errors"])

        return {"copied": copied, "skipped": skipped, "bytes": total_bytes, "errors": errors}
