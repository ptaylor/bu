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
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, ClassVar

from bu.backends.base import Backend, LiveWindow, run_streaming
from bu.filters import build_include_rules, read_filter_lines

# ---------------------------------------------------------------------------
# rsync binary resolution
# ---------------------------------------------------------------------------
_RSYNC_CACHE: str | None = None

# Homebrew (ARM/Intel) and MacPorts install a full rsync here; these are
# checked when PATH only offers Apple's openrsync.
_RSYNC_CANDIDATES: tuple[str, ...] = (
    "/opt/homebrew/bin/rsync",
    "/usr/local/bin/rsync",
    "/opt/local/bin/rsync",
)


def _is_openrsync(binary: str) -> bool:
    """True if ``binary`` is Apple's openrsync (or any pre-3.0 rsync)."""
    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True, text=True, check=False, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    head = (proc.stdout or "").split("\n", 1)[0].strip().lower()
    return "openrsync" in head or head.startswith("rsync  version 2")


def resolve_rsync_binary() -> str:
    """Return the rsync executable to use.

    Order: ``$BU_RSYNC`` override; a full (3.x) rsync on PATH; a known
    Homebrew/MacPorts rsync if PATH only has Apple's openrsync; else
    whatever ``rsync`` resolves to (or the bare name as a last resort).
    """
    global _RSYNC_CACHE
    if _RSYNC_CACHE:
        return _RSYNC_CACHE

    override = os.environ.get("BU_RSYNC", "").strip()
    if override:
        _RSYNC_CACHE = override
        return override

    default = shutil.which("rsync")
    if default and not _is_openrsync(default):
        _RSYNC_CACHE = default
        return default

    for candidate in _RSYNC_CANDIDATES:
        if os.access(candidate, os.X_OK) and not _is_openrsync(candidate):
            _RSYNC_CACHE = candidate
            return candidate

    _RSYNC_CACHE = default or "rsync"
    return _RSYNC_CACHE


def _openrsync_warning(binary: str) -> str | None:
    """Advisory note when the resolved rsync is Apple's openrsync."""
    if _is_openrsync(binary):
        return (
            "rsync is Apple's openrsync (2.x). Socket/fifo files are "
            "skipped (--no-specials); on SMB/NFS shares openrsync cannot "
            "copy socket files (mkstempsock error). Installing a full "
            "rsync ('brew install rsync') or setting BU_RSYNC is "
            "recommended."
        )
    return None


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

    # Method name recorded in status files and reports.
    METHOD_NAME: ClassVar[str] = "rsync"

    @property
    def rsync_binary(self) -> str:
        """Resolved rsync executable (honors BU_RSYNC, prefers a full rsync)."""
        return resolve_rsync_binary()

    def _rsync_notes(self) -> list[str]:
        """Advisory notes about the rsync binary in use."""
        warning = _openrsync_warning(self.rsync_binary)
        return [warning] if warning else []

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
            "method": self.METHOD_NAME,
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
        if extra_args and extra_args.get("restart"):
            # backup-restart: clear the failed/started state and run again.
            self.reset_status()

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

        # One live window shared across all source runs
        window = LiveWindow(
            scroll_lines,
            title=f"Backup output — {self.config.get('_name', '?')}",
        )
        window.__enter__()
        try:
            for idx, src_str in enumerate(source_paths):
                src = Path(src_str).expanduser().resolve()
                window.set_context(str(src))
                include_rules = self._source_include_rules(idx)
                result = self._rsync_one(
                    src,
                    dest / src.name,
                    dry_run,
                    window=window,
                    exclude_from=self._source_exclude_files(idx),
                    include_patterns=include_rules or None,
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
            "notes": self._rsync_notes(),
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

        # One live window for the restore run, with the target on the status line
        window = LiveWindow(
            scroll_lines,
            title=f"Restore output — {self.config.get('_name', '?')}",
        )
        window.__enter__()
        try:
            window.set_context(str(target))
            result = self._rsync_one(
                src,
                target,
                dry_run,
                delete=False,
                window=window,
                exclude=self._status_path().name,
            )
        finally:
            window.__exit__(None, None, None)

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
                [self.rsync_binary, "--version"],
                capture_output=True, text=True, check=False,
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

    def _source_exclude_files(self, index: int) -> list[str]:
        """Exclusion files for a source (per-source, else destination defaults)."""
        per_source = self.config.get("_source_excludes", [])
        if index < len(per_source) and per_source[index] is not None:
            files = per_source[index]
        else:
            files = self.config.get("_exclude_files", [])
        return [p for p in files if isinstance(p, str) and Path(p).is_file()]

    def _source_filter_files(self, index: int) -> tuple[list[str], list[str]]:
        """Return (include_files, exclude_files) as configured for a source.

        A source with no per-source exclude shows the destination-level
        ``_exclude_files`` defaults.
        """
        includes = self.config.get("_source_includes", [])
        excludes = self.config.get("_source_excludes", [])
        inc = list(includes[index]) if index < len(includes) else []
        if index < len(excludes) and excludes[index] is not None:
            exc = list(excludes[index])
        else:
            exc = list(self.config.get("_exclude_files", []))
        return inc, exc

    def _source_include_rules(self, index: int) -> list[str]:
        """rsync include patterns for a source (empty when no include files)."""
        per_source = self.config.get("_source_includes", [])
        if index >= len(per_source):
            return []
        files = [p for p in per_source[index] if isinstance(p, str) and Path(p).is_file()]
        if not files:
            return []
        lines: list[str] = []
        for f in files:
            lines.extend(read_filter_lines(Path(f)))
        return build_include_rules(lines)

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
        link_dest: str | None = None,
        include_patterns: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run rsync for a single source directory → dest subdir.

        ``delete=False`` keeps the restore non-destructive; ``exclude``
        skips a file/directory name (used to keep the bu status file out
        of restores); ``exclude_from`` lists exclusion files to pass via
        ``--exclude-from``; ``link_dest`` adds ``--link-dest=<dir>`` so
        unchanged files are hard-linked from a previous backup (snapshot
        method); ``include_patterns`` adds ``--include`` filter rules and
        a trailing ``--exclude=*`` so only the listed paths are backed up.
        Socket/fifo files are always skipped via ``--no-specials`` (they
        are runtime objects, and SMB/NFS cannot store Unix sockets).
        Output streams live (``-v`` file listing; ``--progress`` bars when
        stdout is a TTY), confined to a window when ``scroll_lines`` > 0.
        Returns ``{files, bytes, errors, stdout, stderr}``.
        """
        # Ensure dest parent exists (rsync can create the leaf)
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)

        # Trailing slash on src: copy *contents*, not the directory itself.
        # --no-specials: skip socket/fifo files (runtime objects; SMB/NFS
        # cannot store Unix sockets and openrsync aborts on them).
        cmd = [self.rsync_binary, "-a", "--no-specials", "-h", "--stats", "-v"]
        if sys.stdout.isatty():
            cmd.append("--progress")
        if delete:
            cmd.append("--delete")
        if exclude:
            cmd.append(f"--exclude={exclude}")
        for ef in exclude_from or []:
            cmd.append(f"--exclude-from={ef}")
        # Include rules come AFTER the exclusion files so exclusions win
        # (first match wins), then a trailing --exclude=* skips the rest.
        if include_patterns:
            cmd.extend(f"--include={p}" for p in include_patterns)
            cmd.append("--exclude=*")
        if dry_run:
            cmd.append("--dry-run")
        if link_dest:
            cmd.append(f"--link-dest={link_dest}")
        # Trailing slash on directories: copy *contents*, not the dir itself.
        cmd.append(f"{src}/" if src.is_dir() else str(src))
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
                if "mkstempsock" in stderr:
                    errors.append(
                        "openrsync failed to copy a socket file (SMB/NFS "
                        "cannot store Unix sockets). bu passes --no-specials "
                        "to skip them — install a full rsync ('brew install "
                        "rsync') if this persists."
                    )

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
            "method": self.METHOD_NAME,
            "config_ok": True,
            "source_paths": source_paths,
            "dest_path": str(dest_dir),
            "dest_exists": dest_dir.is_dir(),
            "exclude_files": list(self.config.get("_exclude_files", [])),
        }

        # Check existence of each source path plus its filter files
        sources_status: list[dict[str, Any]] = []
        for idx, sp in enumerate(source_paths):
            p = Path(sp).expanduser()
            includes, excludes = self._source_filter_files(idx)
            sources_status.append({
                "path": str(p),
                "exists": p.is_dir(),
                "include_files": includes,
                "exclude_files": excludes,
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

        result["notes"] = self._rsync_notes()
        return result

    # --- helpers (only _parse_rsync_stat retained for backup) ---
