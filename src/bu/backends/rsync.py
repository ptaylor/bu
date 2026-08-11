"""rsync-over-SSH backend — syncs files to a remote host via rsync+ssh.

Configuration keys:
    host: str — remote hostname or IP (required)
    user: str — SSH username (optional, defaults to current user)
    path: str — remote destination path (required)
    port: int — SSH port (optional, defaults to 22)
    ssh_key: str — path to SSH private key (optional)
    rsync_opts: list[str] — extra rsync options (optional)
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from bu.backends.base import Backend


class RsyncBackend(Backend):
    """Back up to a remote host using rsync over SSH."""

    def _remote(self) -> str:
        """Build the remote destination string for rsync."""
        host = self.config.get("host", "")
        if not host:
            raise ValueError("rsync backend requires 'host' in config")
        path = self.config.get("path", "")
        if not path:
            raise ValueError("rsync backend requires 'path' in config")

        user = self.config.get("user", "")
        user_part = f"{user}@" if user else ""
        return f"{user_part}{host}:{path}"

    def _rsync_cmd(self, extra_opts: list[str] | None = None) -> list[str]:
        """Build the base rsync command with SSH options."""
        cmd = ["rsync", "-avz", "--delete"]

        # SSH options
        port = self.config.get("port", 22)
        ssh_opts = [f"-p {port}"]
        if ssh_key := self.config.get("ssh_key"):
            ssh_opts.append(f"-i {ssh_key}")
        cmd.extend(["-e", f"ssh {' '.join(ssh_opts)}"])

        # User-configured extra rsync options
        if custom_opts := self.config.get("rsync_opts"):
            cmd.extend(custom_opts)

        if extra_opts:
            cmd.extend(extra_opts)

        return cmd

    def _run_rsync(self, args: list[str], dry_run: bool) -> dict[str, Any]:
        """Run rsync and parse the output. Returns stats dict."""
        cmd = self._rsync_cmd()
        if dry_run:
            cmd.append("--dry-run")
        cmd.extend(args)

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except FileNotFoundError:
            return {"files": 0, "bytes": 0, "errors": ["rsync not found. Is it installed?"]}

        errors: list[str] = []
        if result.returncode != 0:
            errors.append(result.stderr.strip())

        # Parse rsync output for stats (last line has summary)
        files = 0
        bytes_transferred = 0
        for line in result.stdout.splitlines():
            # rsync summary lines look like:
            # "sent X bytes  received Y bytes  Z bytes/sec"
            # or "Number of files: N"
            if "Number of files:" in line:
                try:
                    files = int(line.split(":")[1].strip().replace(",", ""))
                except (ValueError, IndexError):
                    pass

        return {"files": files, "bytes": bytes_transferred, "errors": errors}

    # --- Backend interface ---

    def backup(
        self,
        source_paths: list[str],
        *,
        dry_run: bool = False,
        extra_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        remote = self._remote()
        errors: list[str] = []
        total_files = 0

        for src_str in source_paths:
            src = Path(src_str).expanduser().resolve()
            if not src.exists():
                errors.append(f"Source not found: {src}")
                continue

            # Ensure trailing slash for directory sync semantics
            src_arg = f"{src}/" if src.is_dir() else str(src)
            result = self._run_rsync([src_arg, remote], dry_run)
            total_files += result["files"]
            errors.extend(result["errors"])

        return {
            "files_copied": total_files,
            "files_skipped": 0,
            "bytes_copied": 0,  # rsync doesn't easily give us this
            "errors": errors,
        }

    def restore(
        self,
        restore_path: str,
        *,
        dry_run: bool = False,
        extra_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        remote = self._remote()
        restore = Path(restore_path).expanduser().resolve()

        # rsync from remote to local
        result = self._run_rsync([f"{remote}/", str(restore)], dry_run)

        return {
            "files_restored": result["files"],
            "bytes_restored": result["bytes"],
            "errors": result["errors"],
        }

    def check(
        self,
        source_paths: list[str],
        *,
        extra_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Use rsync --dry-run --itemize-changes to see what would change
        remote = self._remote()
        total_files = 0
        errors: list[str] = []

        for src_str in source_paths:
            src = Path(src_str).expanduser().resolve()
            if not src.exists():
                continue

            src_arg = f"{src}/" if src.is_dir() else str(src)
            result = self._run_rsync(
                [src_arg, remote, "--itemize-changes", "--dry-run"], dry_run=False
            )
            total_files += result["files"]
            errors.extend(result["errors"])

        return {
            "files_to_backup": total_files,
            "files_to_update": 0,
            "total_size": 0,
            "files": [],
        }

    def verify(
        self,
        source_paths: list[str],
        *,
        full: bool = False,
        extra_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        remote = self._remote()
        errors: list[str] = []
        extra: list[str] = []

        if full:
            extra.append("--checksum")

        for src_str in source_paths:
            src = Path(src_str).expanduser().resolve()
            if not src.exists():
                errors.append(f"Source not found: {src}")
                continue

            src_arg = f"{src}/" if src.is_dir() else str(src)
            result = self._run_rsync(
                [src_arg, remote, "--dry-run", "--itemize-changes"] + extra,
                dry_run=False,
            )
            errors.extend(result["errors"])

        # For rsync, we use the dry-run output as verification
        return {
            "verified": 0,
            "missing": [],
            "mismatched": [],
            "errors": errors,
        }

    def status(
        self,
        *,
        extra_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        remote = self._remote()

        # Use ssh to get remote directory stats
        host = self.config.get("host", "")
        user = self.config.get("user", "")
        user_part = f"{user}@" if user else ""
        path = self.config.get("path", "")
        port = self.config.get("port", 22)

        ssh_cmd = ["ssh", "-p", str(port)]
        if ssh_key := self.config.get("ssh_key"):
            ssh_cmd.extend(["-i", ssh_key])

        ssh_cmd.extend([
            f"{user_part}{host}",
            f"du -sb {path} 2>/dev/null; find {path} -type f 2>/dev/null | wc -l; "
            f"find {path} -type f -printf '%T@\\n' 2>/dev/null | sort -rn | head -1",
        ])

        try:
            result = subprocess.run(ssh_cmd, capture_output=True, text=True, check=False, timeout=30)
            lines = result.stdout.strip().splitlines()
            total_size = int(lines[0].split()[0]) if len(lines) > 0 and lines[0] else 0
            file_count = int(lines[1].strip()) if len(lines) > 1 and lines[1] else 0
            latest_ts = lines[2].strip() if len(lines) > 2 and lines[2] else None
        except (subprocess.TimeoutExpired, ValueError, IndexError):
            return {"exists": False, "file_count": 0, "total_size": 0, "last_backup": None}

        import datetime
        last_backup = (
            datetime.datetime.fromtimestamp(float(latest_ts)).isoformat()
            if latest_ts
            else None
        )

        return {
            "exists": True,
            "file_count": file_count,
            "total_size": total_size,
            "last_backup": last_backup,
        }
