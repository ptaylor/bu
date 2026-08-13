"""duplicity method — encrypted incremental backups via the duplicity CLI.

Backs up to a local ``file://`` target: ``<destination>/<name>``.

Passphrase resolution order (backup and restore):
    1. ``passphrase_file`` key in the .toml — file containing the passphrase
       on its first line (permissions should be 0600)
    2. ``BU_PASSPHRASE`` or ``PASSPHRASE`` environment variable
    3. Interactive prompt (getpass) — restore/backup prompt when nothing
       else is configured

Configuration keys:
    destination: str — base target directory for duplicity archives
    passphrase_file: str — optional path to a file holding the passphrase
    full_if_older_than: str — optional duplicity full-backup cadence (e.g. "30D")
"""

from __future__ import annotations

import getpass
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from bu.backends.base import Backend, LiveWindow, run_streaming


class DuplicityMethod(Backend):
    """Back up using the duplicity CLI with symmetric GPG encryption."""

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _target_url(self, subdir: str | None = None) -> str:
        """Return the duplicity target URL: ``file://<destination>/<name>[/<subdir>]``."""
        if not self.config.get("destination"):
            raise ValueError("duplicity method requires 'destination' in config")
        base = Path(self.config["destination"]).expanduser().resolve()
        name = self.config.get("_name", "unknown")
        parts = [base, name]
        if subdir:
            parts.append(subdir)
        return f"file://{Path(*parts)}"

    def _archive_subdirs(self) -> list[str]:
        """List archive subdirectories under the destination (one per source)."""
        base = Path(self.config.get("destination", "")).expanduser().resolve()
        name = self.config.get("_name", "unknown")
        root = base / name
        if not root.is_dir():
            return []
        return sorted(p.name for p in root.iterdir() if p.is_dir())

    def _resolve_passphrase(self) -> str:
        """Resolve the GPG passphrase from file, env, or interactive prompt."""
        name = self.config.get("_name", "unknown")

        # 1. passphrase_file from config
        pf = self.config.get("passphrase_file", "")
        if pf:
            p = Path(pf).expanduser()
            if not p.exists():
                raise ValueError(f"Passphrase file not found: {p}")
            lines = p.read_text().splitlines()
            if not lines:
                raise ValueError(f"Passphrase file is empty: {p}")
            return lines[0].strip()

        # 2. Environment variables
        for env_var in ("BU_PASSPHRASE", "PASSPHRASE"):
            if val := os.environ.get(env_var):
                return val

        # 3. Interactive prompt
        if not sys.stdin.isatty():
            raise ValueError(
                f"No passphrase configured for {name!r}: set 'passphrase_file' "
                "in the config or run interactively to be prompted."
            )
        return getpass.getpass(f"GPG passphrase for '{name}': ")

    def _non_interactive_passphrase(self) -> str | None:
        """Resolve passphrase from file or env only — never prompts."""
        # passphrase_file from config
        pf = self.config.get("passphrase_file", "")
        if pf:
            p = Path(pf).expanduser()
            if p.exists():
                lines = p.read_text().splitlines()
                if lines:
                    return lines[0].strip()

        # Environment variables
        for env_var in ("BU_PASSPHRASE", "PASSPHRASE"):
            if val := os.environ.get(env_var):
                return val

        return None

    def _run_duplicity(
        self,
        args: list[str],
        passphrase: str | None,
        scroll_lines: int = 0,
        window: "LiveWindow | None" = None,
        title: str = "Live output",
    ) -> dict[str, Any]:
        """Run the duplicity CLI.  Returns ``{files, bytes, errors, stdout, stderr}``."""
        env = dict(os.environ)
        if passphrase is not None:
            env["PASSPHRASE"] = passphrase
        else:
            env["PASSPHRASE"] = ""

        cmd = ["duplicity"] + args
        try:
            proc = run_streaming(cmd, env=env, scroll_lines=scroll_lines, window=window, title=title)
        except FileNotFoundError:
            return {"files": 0, "bytes": 0,
                    "errors": ["duplicity binary not found. Is it installed?"],
                    "stdout": "", "stderr": ""}

        errors: list[str] = []
        if proc.returncode != 0:
            stderr = proc.stderr.strip()
            if stderr:
                errors.append(stderr)

        # Parse the "[ Backup Statistics ]" section
        files = self._parse_duplicity_stat(proc.stdout, "NewFiles")
        bytes_backed = self._parse_duplicity_stat(proc.stdout, "NewFileSize")

        return {
            "files": files,
            "bytes": bytes_backed,
            "errors": errors,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }

    @staticmethod
    def _parse_duplicity_stat(output: str, key: str) -> int:
        """Parse an integer value from duplicity's Backup Statistics block."""
        m = re.search(rf"^{key}\s+(\d+)", output, re.MULTILINE)
        if not m:
            return 0
        try:
            return int(m.group(1))
        except ValueError:
            return 0

    def _verbosity(self) -> int:
        """Return the duplicity verbosity level.

        Uses the optional ``verbosity`` config key (0-9); otherwise defaults
        to 6 when stdout is a TTY (file-level logs) and 4 when piped.
        """
        if "verbosity" in self.config:
            try:
                return max(0, min(9, int(self.config["verbosity"])))
            except (TypeError, ValueError):
                pass
        return 6 if sys.stdout.isatty() else 4

    def _duplicity_version(self) -> str:
        """Return the duplicity version string, or empty on failure."""
        try:
            proc = subprocess.run(
                ["duplicity", "--version"], capture_output=True, text=True, check=False,
            )
            return proc.stdout.split("\n")[0].strip()
        except (FileNotFoundError, OSError):
            return ""

    # ------------------------------------------------------------------
    # Backend interface
    # ------------------------------------------------------------------

    def backup(
        self,
        source_paths: list[str],
        *,
        dry_run: bool = False,
        extra_args: dict[str, Any] | None = None,
        scroll_lines: int = 0,
    ) -> dict[str, Any]:
        errors: list[str] = []

        # Validate sources are directories
        for sp in source_paths:
            p = Path(sp).expanduser()
            if not p.exists():
                errors.append(f"Source not found: {p}")
            elif not p.is_dir():
                errors.append(f"Source must be a directory (not a file): {p}")
        if errors:
            return {"files_copied": 0, "files_skipped": 0, "bytes_copied": 0, "errors": errors}

        # Validate destination
        base = Path(self.config.get("destination", "")).expanduser()
        if not base.exists():
            errors.append(f"Destination not found: {base}")
        elif not base.is_dir():
            errors.append(f"Destination is not a directory: {base}")
        if errors:
            return {"files_copied": 0, "files_skipped": 0, "bytes_copied": 0, "errors": errors}

        # Resolve passphrase unless dry-run
        passphrase = None
        if not dry_run:
            try:
                passphrase = self._resolve_passphrase()
            except ValueError as e:
                return {"files_copied": 0, "files_skipped": 0, "bytes_copied": 0, "errors": [str(e)]}

        # duplicity 3.x takes exactly one source per invocation, so run
        # once per source into its own archive subdirectory.
        total_files = 0
        total_bytes = 0
        all_errors: list[str] = []
        all_stdout: list[str] = []
        all_stderr: list[str] = []

        used_subdirs: set[str] = set()
        window = LiveWindow(
            scroll_lines,
            title=f"Backup output — {self.config.get('_name', '?')}",
        )
        window.__enter__()
        try:
            for idx, sp in enumerate(source_paths):
                src = Path(sp).expanduser().resolve()

                # Unique archive subdir per source (basename, deduped)
                subdir = src.name or f"src{idx}"
                candidate = subdir
                n = 2
                while candidate in used_subdirs:
                    candidate = f"{subdir}_{n}"
                    n += 1
                used_subdirs.add(candidate)

                args: list[str] = []
                if cadence := self.config.get("full_if_older_than"):
                    args.append(f"--full-if-older-than={cadence}")
                args.append(f"--verbosity={self._verbosity()}")
                if sys.stdout.isatty():
                    args.append("--progress")
                if dry_run:
                    args.append("--dry-run")
                args.append(str(src))
                args.append(self._target_url(candidate))

                result = self._run_duplicity(args, passphrase, window=window)
                total_files += result["files"]
                total_bytes += result["bytes"]
                all_errors.extend(result["errors"])
                all_stdout.append(result.get("stdout", ""))
                all_stderr.append(result.get("stderr", ""))
        finally:
            window.__exit__(None, None, None)

        return {
            "files_copied": total_files,
            "files_skipped": 0,
            "bytes_copied": total_bytes,
            "errors": all_errors,
            "stdout": "\n".join(all_stdout).strip(),
            "stderr": "\n".join(all_stderr).strip(),
            "duplicity_version": self._duplicity_version(),
        }

    def restore(
        self,
        restore_path: str,
        path_within_backup: str | None = None,
        *,
        dry_run: bool = False,
        extra_args: dict[str, Any] | None = None,
        scroll_lines: int = 0,
    ) -> dict[str, Any]:
        errors: list[str] = []

        restore_dir = Path(restore_path).expanduser()
        if not restore_dir.exists():
            errors.append(f"Restore destination not found: {restore_dir}")
        elif not restore_dir.is_dir():
            errors.append(f"Restore destination is not a directory: {restore_dir}")
        if errors:
            return {"files_restored": 0, "bytes_restored": 0, "errors": errors}

        # Restore always needs the passphrase
        try:
            passphrase = self._resolve_passphrase()
        except ValueError as e:
            return {"files_restored": 0, "bytes_restored": 0, "errors": [str(e)]}

        # Determine which archives to restore
        if path_within_backup:
            archives = [path_within_backup]
        else:
            archives = self._archive_subdirs()

        if not archives:
            return {"files_restored": 0, "bytes_restored": 0,
                    "errors": ["No backup archives found"]}

        total_files = 0
        total_bytes = 0
        all_errors: list[str] = []
        all_stdout: list[str] = []
        all_stderr: list[str] = []

        window = LiveWindow(
            scroll_lines,
            title=f"Restore output — {self.config.get('_name', '?')}",
        )
        window.__enter__()
        try:
            for subdir in archives:
                # With a specific subpath restore directly into restore_dir;
                # otherwise restore each archive into its own subdirectory.
                dest_dir = restore_dir if path_within_backup else restore_dir / subdir
                if not dry_run:
                    dest_dir.mkdir(parents=True, exist_ok=True)

                args = ["restore"]
                args.append(f"--verbosity={self._verbosity()}")
                if sys.stdout.isatty():
                    args.append("--progress")
                if dry_run:
                    args.append("--dry-run")
                args.append(self._target_url(subdir))
                args.append(str(dest_dir))

                result = self._run_duplicity(args, passphrase, window=window)
                total_files += result["files"]
                total_bytes += result["bytes"]
                all_errors.extend(result["errors"])
                all_stdout.append(result.get("stdout", ""))
                all_stderr.append(result.get("stderr", ""))
        finally:
            window.__exit__(None, None, None)

        return {
            "files_restored": total_files,
            "bytes_restored": total_bytes,
            "errors": all_errors,
            "stdout": "\n".join(all_stdout).strip(),
            "stderr": "\n".join(all_stderr).strip(),
            "duplicity_version": self._duplicity_version(),
        }

    def status(
        self,
        *,
        extra_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        base = Path(self.config.get("destination", "")).expanduser()
        name = self.config.get("_name", "unknown")
        source_paths = self.config.get("_source_paths", [])

        result: dict[str, Any] = {
            "destination": name,
            "method": "duplicity",
            "config_ok": True,
            "source_paths": source_paths,
            "dest_path": str(base / name),
            "dest_exists": (base / name).is_dir(),
        }

        # Source existence checks
        sources_status: list[dict[str, Any]] = []
        for sp in source_paths:
            p = Path(sp).expanduser()
            sources_status.append({"path": str(p), "exists": p.is_dir()})
        result["sources"] = sources_status

        # Passphrase availability (file/env only — status never prompts)
        passphrase = self._non_interactive_passphrase()
        result["passphrase_ready"] = passphrase is not None

        # Collection status per archive, if backup exists and passphrase known
        archives = self._archive_subdirs()
        result["archives"] = archives
        if archives and passphrase:
            combined_output: list[str] = []
            combined_errors: list[str] = []
            for subdir in archives:
                r = self._run_duplicity(
                    ["collection-status", self._target_url(subdir)], passphrase,
                )
                combined_output.append(f"--- {subdir} ---")
                combined_output.append(r.get("stdout", "").strip())
                combined_errors.extend(r["errors"])
            result["last_backup"] = {
                "state": "error" if combined_errors else "completed",
                "timestamp": "",
                "output": "\n".join(combined_output)[:500],
                "errors": combined_errors,
            }
        else:
            result["last_backup"] = None

        return result

