"""snapshot method — timestamped mirrors with hard links to previous backups.

Each backup creates a fresh ``<destination>/<YYYY-MM-DD-HH.MM.SS>/``
directory (UTC) containing one subdirectory per source.  Unchanged files
are hard-linked from the previous snapshot via ``rsync --link-dest``, so
many timestamped backups can coexist using roughly the space of the
changes only.  The destination filesystem must support hard links; this
is verified by the create wizard and before the first backup.

Configuration keys required in the .toml file:
    destination: str — base target directory for snapshots
"""

from __future__ import annotations

import datetime
import json
import os
import re
import secrets
import shutil
from pathlib import Path
from typing import Any, ClassVar

from bu.backends.base import LiveWindow
from bu.backends.local import RsyncMethod


def check_hardlink_support(dest: Path) -> list[str]:
    """Verify a destination directory's filesystem supports hard links.

    Writes a random file, hard-links it under another random name, deletes
    the original and checks the link still holds the right content.
    Returns a list of errors (empty when supported).  Used by the create
    wizard and before the first snapshot backup.
    """
    payload = secrets.token_bytes(64)
    p1 = dest / f".bu-linktest-{secrets.token_hex(8)}"
    p2 = dest / f".bu-linktest-{secrets.token_hex(8)}"
    try:
        p1.write_bytes(payload)
        try:
            os.link(p1, p2)
        except OSError as e:
            return [
                (
                    "Destination filesystem does not support hard links "
                    f"({e.strerror or 'os error'}): the snapshot method needs "
                    "them to link unchanged files to previous backups."
                )
            ]
        p1.unlink()
        if p2.read_bytes() != payload:
            return [
                (
                    "Destination filesystem hard-link check failed: the linked "
                    "file's content did not survive deleting the original."
                )
            ]
        return []
    except OSError as e:
        return [f"Destination is not usable for snapshots: {e.strerror or e}"]
    finally:
        p1.unlink(missing_ok=True)
        p2.unlink(missing_ok=True)


class SnapshotMethod(RsyncMethod):
    """Back up into UTC-timestamped snapshots linked to the previous one.

    Sources are mirrored into ``<destination>/<timestamp>/<source name>/``;
    ``rsync --link-dest`` points at the newest snapshot containing that
    source, so unchanged files become hard links instead of copies.
    """

    METHOD_NAME: ClassVar[str] = "snapshot"

    # Timestamp directory names: 2026-08-15-21.09.41, with an optional
    # -2/-3 suffix for same-second collisions.
    SNAPSHOT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{2}\.\d{2}\.\d{2}(-\d+)?$")

    # ------------------------------------------------------------------
    # snapshot bookkeeping
    # ------------------------------------------------------------------

    def _timestamp_name(self) -> str:
        """Return a fresh UTC timestamp directory name (unique in the dest)."""
        base = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d-%H.%M.%S")
        name = base
        n = 2
        while (self._dest_dir() / name).exists():
            name = f"{base}-{n}"
            n += 1
        return name

    def _list_snapshots(self) -> list[Path]:
        """Return sorted snapshot directories (oldest first)."""
        dest = self._dest_dir()
        if not dest.is_dir():
            return []
        return sorted(
            p for p in dest.iterdir()
            if p.is_dir() and self.SNAPSHOT_RE.match(p.name)
        )

    def _latest_snapshot_with(self, subdir: str, exclude: Path | None = None) -> Path | None:
        """Return the newest snapshot containing ``subdir``, if any.

        ``exclude`` skips a snapshot dir (used when resuming a failed run
        so the in-progress dir is never its own ``--link-dest`` base).
        """
        for snap in reversed(self._list_snapshots()):
            if snap == exclude:
                continue
            if (snap / subdir).is_dir():
                return snap
        return None

    def _resume_snapshot(self) -> str | None:
        """Return the timestamp of an interrupted run, if recoverable.

        Prefers the ``snapshot`` key from the status file; older status
        files (written before that key existed) may lack it, in which case
        the newest snapshot directory is assumed to be the interrupted one.
        """
        sp = self._status_path()
        if sp.exists():
            try:
                data = json.loads(sp.read_text())
            except (json.JSONDecodeError, OSError):
                data = {}
            ts = data.get("snapshot")
            if isinstance(ts, str) and self.SNAPSHOT_RE.match(ts):
                return ts
        snaps = self._list_snapshots()
        return snaps[-1].name if snaps else None

    def incomplete_snapshot_info(self) -> dict[str, Any] | None:
        """Return the removable incomplete snapshot, or None.

        Only a non-``completed`` run with a ``snapshot`` timestamp whose
        directory still exists is removable (completed snapshots are never
        offered for removal by this helper).
        """
        sp = self._status_path()
        if not sp.exists():
            return None
        try:
            data = json.loads(sp.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        if data.get("state") == "completed":
            return None
        ts = data.get("snapshot")
        if not isinstance(ts, str) or not self.SNAPSHOT_RE.match(ts):
            return None
        snap_dir = self._dest_dir() / ts
        if not snap_dir.is_dir():
            return None
        return {"snapshot": ts, "dir": str(snap_dir)}

    def remove_incomplete_snapshot(self) -> dict[str, Any]:
        """Delete the incomplete snapshot directory and reset the status.

        Only the single timestamped directory is removed — older snapshots
        are never touched.  Returns an ok/errors result.
        """
        info = self.incomplete_snapshot_info()
        if info is None:
            return {"ok": False, "errors": ["No incomplete snapshot backup found"]}
        try:
            shutil.rmtree(Path(info["dir"]))
        except OSError as e:
            return {"ok": False, "errors": [f"Cannot remove {info['dir']}: {e}"]}
        self.reset_status()
        return {"ok": True, "removed": info["dir"], "snapshot": info["snapshot"]}

    # ------------------------------------------------------------------
    # hard-link support check
    # ------------------------------------------------------------------

    def _check_hardlink_support(self) -> list[str]:
        """Run the hard-link support check against this destination."""
        return check_hardlink_support(self._dest_dir())

    def _hardlink_check_needed(self) -> bool:
        """True when this destination hasn't been verified for hard links.

        A ``bu-<name>-status.txt`` written by the snapshot method records
        an earlier successful check, so later backups skip re-checking.
        Status files from other methods don't count.
        """
        sp = self._status_path()
        if not sp.exists():
            return True
        try:
            return json.loads(sp.read_text()).get("method") != self.METHOD_NAME
        except (json.JSONDecodeError, OSError):
            return True

    # ------------------------------------------------------------------
    # backup action
    # ------------------------------------------------------------------

    def backup(
        self,
        source_paths: list[str],
        *,
        dry_run: bool = False,
        extra_args: dict[str, Any] | None = None,
        scroll_lines: int = 0,
    ) -> dict[str, Any]:
        """Mirror each source into a fresh timestamped snapshot.

        The newest previous snapshot holding the same source subdirectory
        is passed as ``--link-dest``; unchanged files are hard-linked
        rather than copied.  Dry runs create nothing.
        """
        resume: str | None = None
        if extra_args and extra_args.get("restart"):
            # backup-restart: remember the failed run's timestamp before
            # clearing the status, then continue into the SAME directory.
            resume = self._resume_snapshot()
            self.reset_status()

        errors = self._validate_sources_are_dirs(source_paths)
        if errors:
            return {"files_copied": 0, "files_skipped": 0, "bytes_copied": 0, "errors": errors}

        dest_errors = self._validate_destination()
        if dest_errors:
            return {"files_copied": 0, "files_skipped": 0, "bytes_copied": 0, "errors": dest_errors}

        ts = self._timestamp_name()
        if resume and (self._dest_dir() / resume).is_dir():
            # Continue into the same timestamped directory the failed run
            # was filling; a missing dir falls back to a fresh timestamp.
            ts = resume
        snap_dir = self._dest_dir() / ts

        if not dry_run:
            if self._hardlink_check_needed():
                link_errors = self._check_hardlink_support()
                if link_errors:
                    return {"files_copied": 0, "files_skipped": 0, "bytes_copied": 0,
                            "errors": link_errors}
            # Record the snapshot timestamp from the very start so an
            # interrupted run can be resumed into the SAME directory.
            self._write_status("started", snapshot=ts)
            snap_dir.mkdir(parents=True, exist_ok=True)

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
            used_subdirs: set[str] = set()
            for idx, src_str in enumerate(source_paths):
                src = Path(src_str).expanduser().resolve()

                # Unique snapshot subdir per source (basename, deduped).
                subdir = src.name or f"src{idx}"
                candidate = subdir
                n = 2
                while candidate in used_subdirs:
                    candidate = f"{subdir}_{n}"
                    n += 1
                used_subdirs.add(candidate)

                # Hard-link unchanged files from the newest snapshot that
                # already holds this source's subdirectory (never the
                # in-progress dir itself when resuming).
                link_dest = None
                if latest := self._latest_snapshot_with(candidate, exclude=snap_dir):
                    link_dest = str(latest / candidate)

                window.set_context(str(src))
                include_rules = self._source_include_rules(idx)
                result = self._rsync_one(
                    src,
                    snap_dir / candidate,
                    dry_run,
                    window=window,
                    exclude_from=self._source_exclude_files(idx),
                    include_patterns=include_rules or None,
                    link_dest=link_dest,
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
                    snapshot=ts,
                    files_copied=total_files,
                    bytes_copied=total_bytes,
                    errors=all_errors,
                )
            else:
                self._write_status(
                    "completed",
                    snapshot=ts,
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
            "snapshot": ts,
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
        """Restore from the latest snapshot (or a timestamped one).

        ``path_within_backup`` starting with a timestamp selects that
        snapshot; otherwise the path is taken relative to the newest
        snapshot.  Files are restored into ``RESTORE_DIR/<final path
        component>``.  Restore never deletes files.
        """
        errors: list[str] = []
        restore_dir = Path(restore_path).expanduser()
        if not restore_dir.exists():
            errors.append(f"Restore destination not found: {restore_dir}")
        elif not restore_dir.is_dir():
            errors.append(f"Restore destination is not a directory: {restore_dir}")
        if errors:
            return {"files_restored": 0, "bytes_restored": 0, "errors": errors}

        snapshots = self._list_snapshots()
        if not snapshots:
            return {"files_restored": 0, "bytes_restored": 0,
                    "errors": [f"No snapshots found at {self._dest_dir()}"]}

        snap = snapshots[-1]  # newest
        inner: str | None = None
        if path_within_backup:
            parts = Path(path_within_backup).parts
            first = parts[0]
            if self.SNAPSHOT_RE.match(first):
                snap = self._dest_dir() / first
                if not snap.is_dir():
                    names = ", ".join(p.name for p in reversed(snapshots))
                    return {"files_restored": 0, "bytes_restored": 0,
                            "errors": [f"Snapshot {first!r} not found. Available: {names}"]}
                inner = "/".join(parts[1:]) or None
            else:
                inner = "/".join(parts)

        src = snap / inner if inner else snap
        target = restore_dir / Path(inner).name if inner else restore_dir

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
            )
        finally:
            window.__exit__(None, None, None)

        return {
            "files_restored": result["files"],
            "bytes_restored": result["bytes"],
            "errors": result["errors"],
            "stdout": result.get("stdout", ""),
            "stderr": result.get("stderr", ""),
            "snapshot": snap.name,
            "rsync_version": self._rsync_version(),
        }

    # ------------------------------------------------------------------
    # status action
    # ------------------------------------------------------------------

    def status(
        self,
        *,
        extra_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return config, snapshot list, and last backup details."""
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
        }

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

        snapshots = self._list_snapshots()
        result["snapshots"] = [p.name for p in snapshots]
        result["snapshot_count"] = len(snapshots)
        result["latest_snapshot"] = snapshots[-1].name if snapshots else None

        sp = self._status_path()
        if sp.exists():
            try:
                result["last_backup"] = json.loads(sp.read_text())
            except (json.JSONDecodeError, OSError) as e:
                result["status_file_error"] = str(e)
        else:
            result["last_backup"] = None

        return result
