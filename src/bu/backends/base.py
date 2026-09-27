"""Abstract base class for all backup backends."""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any, Self

from bu.filters import last_entry, marker_filename, restore_test_warnings

try:  # POSIX only — bu targets macOS and Linux
    import resource as _resource
except ImportError:  # pragma: no cover - non-POSIX platforms
    _resource = None  # type: ignore[assignment]

# ANSI CSI escape sequences (e.g. colours, cursor movement) stripped from rows.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


class StreamResult:
    """Container for a streamed subprocess result."""

    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class LiveWindow:
    """Docker-style fixed-height live output window with a status line.

    Keeps the last N lines of streamed output, redrawing them in place
    using ANSI cursor movement (clear-line + cursor-up).  Rows are
    colour-coded (errors red, warnings yellow, progress cyan, dirs blue).
    A status line below the window shows the elapsed time plus an
    activity label set via ``set_context`` (e.g. the source path being
    backed up), repainted at most every ``status_interval`` seconds.
    A yellow ``─`` dividing rule sits between the window rows and the
    status line, matching the title bar.
    Rows are sanitised (ANSI escape codes stripped, backspaces emulated)
    so tools like duplicity can't corrupt the display; the terminal width
    is re-read on every repaint so a resize doesn't scramble the layout.
    Rows are capped at width-1 and end in \r\n, and terminal auto-wrap is
    disabled while the window is active (restored on exit), so a wide row
    can never wrap into the rows below it.
    Auto-disabled when stdout is not a TTY (plain passthrough).
    """

    def __init__(
        self,
        lines: int,
        status_interval: float = 1.0,
        title: str = "Live output",
    ) -> None:
        self.lines = lines
        self.status_interval = status_interval
        self.title = title
        self.active = False
        self._lock = threading.Lock()
        self._buf: list[str] = []
        self._context = ""
        self._painted = 0
        self._width = 80
        self._start = 0.0
        self._last_status_paint = -1.0
        self._stop: threading.Event | None = None
        self._ticker: threading.Thread | None = None

    # -- context management ---------------------------------------------

    def __enter__(self) -> Self:
        if self.lines <= 0 or not sys.stdout.isatty() or self.active:
            return self
        try:
            size = shutil.get_terminal_size()
        except OSError:
            return self
        self._width = size.columns
        if self._width < 20 or size.lines < self.lines + 3:
            return self
        # Title bar above the window: "── Title ──────…"
        rule = "─" * max(0, self._width - len(self.title) - 3)
        sys.stdout.write(f"\033[1;33m─ {self.title} {rule}\033[0m\r\n")
        # Reserve window + dividing rule + status rows, then move cursor back
        sys.stdout.write("\n" * (self.lines + 2))
        sys.stdout.write(f"\033[{self.lines + 2}A")
        # Disable terminal auto-wrap: if a row ever exceeds the width (stale
        # terminal size, resize race), the terminal truncates it at the right
        # margin instead of wrapping and scrambling the fixed-height block.
        sys.stdout.write("\033[?7l")
        sys.stdout.flush()
        self._start = time.monotonic()
        self._last_status_paint = -1.0
        self.active = True
        self._stop = threading.Event()
        self._ticker = threading.Thread(target=self._tick, name="bu-livewindow", daemon=True)
        self._ticker.start()
        return self

    def _tick(self) -> None:
        """Background repaint loop.

        Keeps the status line's elapsed-time clock advancing even when the
        streamed process goes quiet (e.g. duplicity uploading a volume emits
        nothing for minutes at a time).
        """
        while self.active and not self._stop.wait(self.status_interval):
            with self._lock:
                if not self.active:
                    break
                try:
                    self._redraw()
                except OSError:
                    # stdout went away (closed terminal) — stop repainting.
                    self.active = False
                    break

    def __exit__(self, *exc) -> bool:
        if not self.active:
            return False
        if self._stop is not None:
            self._stop.set()
        if self._ticker is not None:
            self._ticker.join(timeout=max(1.0, self.status_interval * 2))
        with self._lock:
            self._redraw()
            # The cursor sits on the status row after a redraw;
            # move below it so the final summary prints fresh.
            sys.stdout.write("\033[?7h")   # restore terminal auto-wrap
            sys.stdout.write("\n")
            sys.stdout.flush()
            self.active = False
        return False

    def flush(self) -> None:
        """No-op flush to satisfy file-like interface (write() already flushes)."""

    # -- output API -----------------------------------------------------

    def write(self, text: str) -> None:
        """Write streamed output.  Falls back to plain passthrough off-TTY."""
        if not self.active:
            sys.stdout.write(text)
            sys.stdout.flush()
            return
        with self._lock:
            # Split on newlines AND carriage returns: progress bars update
            # with \r, and each update becomes its own display row.  ANSI
            # escape codes and backspaces are sanitised per row.
            raw_rows = [r for r in re.split(r"[\r\n]", text) if r]
            rows = [c for c in (self._clean_row(r) for r in raw_rows) if c]
            if rows:
                self._buf.extend(rows)
                if len(self._buf) > self.lines:
                    del self._buf[: len(self._buf) - self.lines]
                self._redraw()

    def set_context(self, text: str) -> None:
        """Set the status line's activity label (e.g. the source path)."""
        with self._lock:
            self._context = text
            if self.active:
                # Reset the throttle so the new context paints immediately.
                self._last_status_paint = -1.0
                self._redraw()

    @staticmethod
    def _colour_row(row: str) -> str:
        """Colour-code a display row by its content."""
        low = row.lower()
        if any(w in low for w in ("error", "failed", "denied", "not found")):
            return f"\033[31m{row}\033[0m"          # red
        if any(w in low for w in ("warn", "skip")):
            return f"\033[33m{row}\033[0m"          # yellow
        if any(w in low for w in ("xfer#", "100%", "eta ", "speedup")):
            return f"\033[36m{row}\033[0m"          # cyan
        if row.endswith("/") or row == "./":
            return f"\033[34m{row}\033[0m"          # blue
        if row.startswith(("Transfer starting", "Number of")):
            return f"\033[32m{row}\033[0m"          # green
        return row

    @staticmethod
    def _clean_row(row: str) -> str:
        """Strip ANSI escape sequences and emulate backspace erasing.

        duplicity's progress bars write ``\r`` + backspace re-draws; the
        raw bytes would otherwise corrupt the window rows.
        """
        row = _ANSI_RE.sub("", row)
        out: list[str] = []
        for ch in row:
            if ch == "\b":
                if out:
                    out.pop()
            else:
                out.append(ch)
        return "".join(out).rstrip()

    def _redraw(self) -> None:
        # Re-read the terminal width: a resize mid-run would otherwise
        # scramble row truncation and the divider/status painting.
        try:
            self._width = shutil.get_terminal_size().columns
        except OSError:
            pass
        if self._width < 20:
            return
        # Move cursor up to the first output row of the window block.
        if self._painted:
            sys.stdout.write(f"\033[{self._painted + 1}A")
        # Always paint exactly `lines` output rows (blank-filling the rest).
        # Rows are capped at width-1 and terminated with \r\n: a row filling
        # the last column would leave the cursor wrap-pending, shifting every
        # following row down a line and scrambling the window block.
        for i in range(self.lines):
            sys.stdout.write("\033[2K")       # erase whole line
            if i < len(self._buf):
                sys.stdout.write(self._colour_row(self._buf[i][: max(0, self._width - 1)]))
            sys.stdout.write("\r\n")
        self._painted = self.lines
        # Yellow dividing rule above the status line, matching the title bar
        sys.stdout.write("\033[2K")
        sys.stdout.write(f"\033[1;33m{'─' * max(0, self._width - 1)}\033[0m")
        sys.stdout.write("\r\n")
        # Status line below the window — throttled to update infrequently
        now = time.monotonic()
        if self._last_status_paint < 0 or now - self._last_status_paint >= self.status_interval:
            elapsed = now - self._start
            status = f"[{int(elapsed) // 60:02d}:{int(elapsed) % 60:02d}] {self._context}"
            sys.stdout.write("\033[2K")
            sys.stdout.write(f"\033[1;36m{status[: max(0, self._width - 1)]}\033[0m")
            self._last_status_paint = now
        sys.stdout.flush()


#: Minimum soft open-file limit the external tools need.  duplicity 3.x
#: aborts any full/incremental/restore run with "Max open files of N is too
#: low, should be >= 1024" when the soft ``RLIMIT_NOFILE`` is smaller, and
#: macOS hands 256 to processes started from Finder, launchd or cron.
MIN_OPEN_FILE_LIMIT = 1024


def on_disk_name(parent: Path, name: str) -> str:
    """Return ``name`` as spelled inside ``parent``, or ``name`` when unknown."""
    try:
        for entry in os.scandir(parent):
            if entry.name.lower() == name.lower():
                return entry.name
    except OSError:
        pass
    return name


def on_disk_path(path: Path) -> Path:
    """Return ``path`` with every existing component spelled as it is on disk.

    macOS volumes are case-insensitive but case-*preserving*, so a configured
    ``.../Dropbox/backups`` can name a directory that is actually called
    ``Backups`` — and ``Path.resolve()`` never corrects the spelling.  The
    backup copies the on-disk name, so recording the configured spelling would
    build a path that does not exist whenever the destination is a
    case-sensitive volume (APFS can be, and often is on external disks).
    Components that do not exist keep the given spelling: bu creates them.
    Symlinks are resolved first, so the result stays comparable with the
    resolved source paths (``/tmp`` and ``/private/tmp`` are the same place).
    """
    expanded = path.expanduser().resolve()
    current = Path(expanded.anchor)
    for part in expanded.relative_to(expanded.anchor).parts:
        candidate = current / part
        if candidate.exists():
            current = current / on_disk_name(current, part)
        else:
            current = candidate
    return current


def ensure_open_file_limit(minimum: int = MIN_OPEN_FILE_LIMIT) -> bool:
    """Raise the soft ``RLIMIT_NOFILE`` to at least ``minimum`` (best effort).

    Subprocesses inherit the limit, so raising it once before spawning a
    tool is enough.  Never lowers an existing (higher) limit, never touches
    the hard limit, and never raises — the hard limit may simply be too low
    for the request to be satisfiable.  Returns True when the soft limit is
    (now) at least ``minimum``, i.e. when duplicity would accept it.
    """
    if _resource is None:  # pragma: no cover - non-POSIX platforms
        return True
    try:
        soft, hard = _resource.getrlimit(_resource.RLIMIT_NOFILE)
    except (OSError, ValueError):  # pragma: no cover - defensive
        return True
    if soft != _resource.RLIM_INFINITY and soft < minimum:
        target = minimum if hard == _resource.RLIM_INFINITY else min(minimum, hard)
        try:
            _resource.setrlimit(_resource.RLIMIT_NOFILE, (target, hard))
        except (OSError, ValueError):  # pragma: no cover - hard limit too low
            return False
        soft = target
    return soft == _resource.RLIM_INFINITY or soft >= minimum


def run_streaming(
    cmd: list[str],
    env: dict[str, str] | None = None,
    scroll_lines: int = 0,
    window: LiveWindow | None = None,
    title: str = "Live output",
    silence_stderr: bool = False,
    silence_stdout: bool = False,
) -> StreamResult:
    """Run a command, streaming its output live to the terminal while capturing.

    stdout/stderr are written to the terminal as they arrive AND collected
    for later logging.  If ``scroll_lines`` > 0 and stdout is a TTY, output
    is confined to a Docker-style live window of that height; otherwise
    output passes through as plain text.

    If ``window`` is supplied, it is used instead of creating one — the
    caller owns its lifecycle (useful for sharing one window across
    several subprocess runs).  ``silence_stderr`` captures stderr without
    writing it anywhere (the caller can re-emit condensed lines later);
    ``silence_stdout`` does the same for stdout (the caller keeps it
    captured, e.g. duplicity collection-status during ``bu status``).
    Return a StreamResult with combined output strings.
    """
    owns_window = window is None
    if window is None:
        window = LiveWindow(scroll_lines, title=title)
    stdout_window = None if silence_stdout else window
    stderr_window = None if silence_stderr else window
    if owns_window:
        window.__enter__()
    try:
        # duplicity refuses to run below 1024 open files (macOS gives 256 to
        # GUI/launchd processes); every child inherits whatever we set here.
        ensure_open_file_limit()
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )

        captured: dict[str, list[str]] = {"stdout": [], "stderr": []}

        def _tee(stream, target, bucket):
            try:
                while True:
                    # Read in chunks, NOT lines: progress bars update with \r
                    # and no newline, which would stall readline() forever.
                    chunk = stream.read(4096)
                    if not chunk:
                        break
                    if target is not None:
                        target.write(chunk)
                        target.flush()
                    bucket.append(chunk)
            except (UnicodeDecodeError, ValueError, OSError):
                # Binary/garbled output — skip rather than crash the thread
                pass
            finally:
                stream.close()

        t1 = threading.Thread(target=_tee, args=(proc.stdout, stdout_window, captured["stdout"]))
        t2 = threading.Thread(target=_tee, args=(proc.stderr, stderr_window, captured["stderr"]))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        returncode = proc.wait()
    finally:
        if owns_window:
            window.__exit__(None, None, None)

    return StreamResult(returncode, "".join(captured["stdout"]), "".join(captured["stderr"]))


def run_filtered(
    cmd: list[str],
    *,
    env: dict[str, str] | None = None,
    on_line: Callable[[str], None],
) -> int:
    """Run a command, handing each merged output line to ``on_line``.

    Returns the exit status.  Unlike ``run_streaming`` nothing is retained:
    this suits commands whose output is *data* rather than status for the live
    window — duplicity's verbosity-9 listing log runs to hundreds of megabytes
    for a large source, which is far too much to buffer.
    """
    ensure_open_file_limit()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    stream = proc.stdout
    try:
        if stream is not None:
            for raw in stream:
                on_line(raw.rstrip("\n"))
    finally:
        if stream is not None:
            stream.close()
        if proc.poll() is None:
            # The consumer bailed out (e.g. Ctrl-C); don't leave it running.
            proc.terminate()
    return proc.wait()


class Backend(ABC):
    """Abstract interface that all backup backends must implement."""

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialise the backend with its configuration dictionary.

        The config dict contains the ``destination`` path plus any extra
        keys from the TOML destination entry (excluding reserved keys).
        Internal keys ``_name`` and ``_source_paths`` are also provided.
        """
        self.config = config

    def backup_block_reason(self) -> str | None:
        """Return why a new backup is blocked, or None to proceed.

        The status file records the last run; only a ``completed`` state
        (or no status file at all) allows a new backup, so an ongoing or
        failed run is never silently overwritten.
        """
        name = self.config.get("_name", "unknown")
        sp = self._status_path()
        if not sp.exists():
            return None
        try:
            data = json.loads(sp.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
        state = data.get("state")
        if state == "completed":
            return None
        if state == "started":
            return f"Backup for {name!r} appears to still be running (state 'started')."
        return f"Previous backup for {name!r} did not complete (state {state or 'unknown'!r})."

    def restart_block_reason(self) -> str | None:
        """Return why backup-restart should be refused, or None to proceed.

        Restart only makes sense while a previous run is 'started' or
        'error'; a completed or missing status has nothing to recover.
        """
        name = self.config.get("_name", "unknown")
        sp = self._status_path()
        if not sp.exists():
            return (
                f"No previous backup state to restart for {name!r} — "
                f"run 'bu backup {name}' instead."
            )
        try:
            data = json.loads(sp.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
        if data.get("state") == "completed":
            return f"Backup for {name!r} already completed — nothing to restart."
        return None

    def reset_status(self) -> None:
        """Delete the status file so the next backup starts unblocked."""
        self._status_path().unlink(missing_ok=True)

    def preflight_notes(self) -> list[str]:
        """Advisory warnings to show before a backup starts.

        Defaults to the restore-test configuration checks.  Backends override
        this for configuration footguns that don't block the run (e.g.
        exclusion patterns that rsync would silently ignore) and must chain to
        ``super()`` so these survive.
        """
        return self.restore_test_notes()

    # ------------------------------------------------------------------
    # restore-test markers (bu restore-test)
    # ------------------------------------------------------------------

    def restore_test_dirs(self) -> list[str]:
        """Configured restore-test directories (absolute paths), if any."""
        return [str(p) for p in self.config.get("_restore_test_dirs", [])]

    def _source_subdirs(self, source_paths: list[str]) -> dict[int, str]:
        """Map source index -> the subdirectory the backup uses for it.

        Applies the same basename dedup (``_2``, ``_3``) the backends apply, so
        a marker's path inside the backup always names the right archive or
        snapshot subdirectory.
        """
        used: set[str] = set()
        subdirs: dict[int, str] = {}
        for idx, sp in enumerate(source_paths):
            base = Path(sp).expanduser().name or f"src{idx}"
            candidate = base
            n = 2
            while candidate in used:
                candidate = f"{base}_{n}"
                n += 1
            used.add(candidate)
            subdirs[idx] = candidate
        return subdirs

    def resolve_restore_markers(
        self, source_paths: list[str],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Map each restore-test directory to the source tree that owns it.

        Returns ``(markers, warnings)``.  A directory outside every source tree
        becomes a warning and is skipped — never an error, so a stale entry
        cannot stop a backup.  Where sources are nested (Dropbox lives inside
        ``$HOME``) the longest matching source path wins, so the marker is
        verified against the archive that actually receives it.
        """
        name = str(self.config.get("_name", "unknown"))
        marker_file = marker_filename(name)
        sources = [
            (idx, Path(sp).expanduser().resolve())
            for idx, sp in enumerate(source_paths)
        ]
        # Containment is decided on the directories' real on-disk names, which
        # is also what the backup records (see on_disk_path).
        canonical = [(idx, on_disk_path(src)) for idx, src in sources]
        subdirs = self._source_subdirs(source_paths)

        markers: list[dict[str, Any]] = []
        warnings: list[str] = []
        for raw in self.restore_test_dirs():
            display = Path(raw).expanduser()
            real = on_disk_path(display)
            owner: tuple[int, Path] | None = None
            for idx, src in canonical:
                if real != src and src not in real.parents:
                    continue
                if owner is None or len(str(src)) > len(str(owner[1])):
                    owner = (idx, src)
            if owner is None:
                warnings.append(
                    f"restore-test directory {display} is not inside any source path "
                    f"of {name!r} — it will be skipped"
                )
                continue
            idx, src = owner
            rel = real.relative_to(src)
            markers.append({
                "dir": str(real),
                "path": str(real / marker_file),
                "source": str(src),
                "index": idx,
                "source_subdir": subdirs[idx],
                "rel_dir": "" if rel == Path(".") else rel.as_posix(),
                "marker_file": marker_file,
            })
        return markers, warnings

    def _restore_test_exclude_files(self, index: int) -> list[str]:
        """Own + destination-level exclusion files for a source (may not exist)."""
        per_source = self.config.get("_source_excludes", [])
        own = per_source[index] if index < len(per_source) and per_source[index] else []
        ordered: list[str] = []
        for path in [*own, *self.config.get("_exclude_files", [])]:
            if isinstance(path, str) and path not in ordered:
                ordered.append(path)
        return ordered

    def restore_test_notes(self) -> list[str]:
        """Warnings about the restore-test configuration (never blocking)."""
        markers, warnings = self.resolve_restore_markers(
            self.config.get("_source_paths", []),
        )
        for marker in markers:
            idx = marker["index"]
            includes = self.config.get("_source_includes", [])
            own_includes = includes[idx] if idx < len(includes) else []
            warnings.extend(restore_test_warnings(
                marker_file=marker["marker_file"],
                rel_dir=marker["rel_dir"],
                include_files=[str(p) for p in own_includes or []],
                exclude_files=self._restore_test_exclude_files(idx),
            ))
        return warnings

    def restore_test_run_id(self) -> str:
        """Value identifying this run, appended to every marker file.

        The snapshot backend overrides this with the snapshot directory name,
        so a marker inside a snapshot always names that snapshot.
        """
        return datetime.datetime.now(datetime.timezone.utc).isoformat()

    def write_restore_markers(
        self, markers: list[dict[str, Any]], value: str,
    ) -> list[str]:
        """Create each marker directory and append ``value`` to its marker file.

        The append is skipped when the last line is already ``value``, so a
        resumed run (``bu backup-restart`` reuses the snapshot timestamp) or a
        directory claimed by two nested sources never duplicates an entry.
        Returns error strings; a failed marker is reported but not fatal.
        """
        errors: list[str] = []
        for marker in markers:
            path = Path(marker["path"])
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.is_file() and last_entry(path.read_text(errors="replace")) == value:
                    continue
                with open(path, "a") as fh:
                    fh.write(value + "\n")
            except OSError as e:
                errors.append(
                    f"Cannot write restore-test marker {path}: {e.strerror or e}"
                )
        return errors

    def restore_test_record(
        self, markers: list[dict[str, Any]], run_id: str,
    ) -> list[dict[str, Any]]:
        """Compact marker records for the status file (what to verify later)."""
        return [
            {
                "dir": m["dir"],
                "source": m["source"],
                "source_subdir": m["source_subdir"],
                "rel_dir": m["rel_dir"],
                "marker_file": m["marker_file"],
                "run_id": run_id,
            }
            for m in markers
        ]

    def read_status(self) -> dict[str, Any]:
        """Return the parsed status file, or ``{}`` when missing/unreadable."""
        path = self._status_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def _restore_test_backup_path(
        self, entry: dict[str, Any], status: dict[str, Any],
    ) -> str:
        """Path of a marker inside the backup, as ``bu restore`` expects it."""
        parts = [
            entry.get("source_subdir") or "",
            str(entry.get("rel_dir") or ""),
            entry.get("marker_file") or "",
        ]
        return "/".join(p for p in parts if p)

    def restore_test(self, *, scroll_lines: int = 0) -> dict[str, Any]:
        """Restore every marker file and check its newest entry.

        Reads the marker list and run id the last backup recorded, restores
        each marker into a private temporary directory, and passes when the
        file arrives and its newest entry is that run id.  The temporary
        directory is always removed — the findings come back in ``results``
        instead.  Destinations that need a passphrase resolve it without
        prompting, so this is safe to run unattended.
        """
        name = str(self.config.get("_name", "unknown"))
        status = self.read_status()
        entries = status.get("restore_test") or []
        expected = str(status.get("run_id") or "")

        result: dict[str, Any] = {
            "ok": True,
            "checked": 0,
            "passed": 0,
            "failed": 0,
            "expected": expected,
            "results": [],
            "errors": [],
        }
        if not self.restore_test_dirs():
            result["errors"] = ["No restore-test directories configured."]
            return result
        if not entries:
            result["ok"] = False
            result["errors"] = [
                (
                    f"Last backup of {name!r} recorded no restore-test markers — "
                    f"run 'bu backup {name}' first."
                )
            ]
            return result
        if not expected:
            result["ok"] = False
            result["errors"] = [
                (
                    f"Last backup of {name!r} recorded no run id — "
                    f"re-run 'bu backup {name}'."
                )
            ]
            return result

        scratch = Path(tempfile.mkdtemp(prefix=f"bu-restore-test-{name}-"))
        try:
            for position, entry in enumerate(entries):
                outcome = self._verify_restore_test_entry(
                    entry, expected, status, scratch, position,
                )
                result["results"].append(outcome)
                if not outcome["ok"]:
                    result["errors"].append(f"{outcome['path']}: {outcome['error']}")
            result["checked"] = len(result["results"])
            result["passed"] = sum(1 for r in result["results"] if r["ok"])
            result["failed"] = result["checked"] - result["passed"]
            result["ok"] = result["failed"] == 0
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        return result

    def _verify_restore_test_entry(
        self,
        entry: dict[str, Any],
        expected: str,
        status: dict[str, Any],
        scratch: Path,
        position: int,
    ) -> dict[str, Any]:
        """Restore one marker into ``scratch`` and compare its newest entry."""
        backup_path = self._restore_test_backup_path(entry, status)
        marker_file = str(entry.get("marker_file", ""))
        outcome: dict[str, Any] = {
            "dir": entry.get("dir", "?"),
            "path": str(Path(str(entry.get("dir", ""))).expanduser() / marker_file),
            "backup_path": backup_path,
            "expected": expected,
            "actual": None,
            "ok": False,
            "error": None,
        }
        restore_dir = scratch / str(position)
        restore_dir.mkdir(parents=True, exist_ok=True)
        restored = self.restore(
            str(restore_dir),
            backup_path,
            extra_args={"non_interactive": True, "quiet": True},
        )
        if restore_errors := restored.get("errors"):
            outcome["error"] = "; ".join(str(e) for e in restore_errors)
            return outcome
        target = restore_dir / Path(backup_path).name
        if not target.is_file():
            outcome["error"] = f"marker file was not restored (expected {target})"
            return outcome
        actual = last_entry(target.read_text(errors="replace"))
        outcome["actual"] = actual
        if actual == expected:
            outcome["ok"] = True
        else:
            outcome["error"] = (
                f"last entry {actual or '(empty)'} does not match the run id {expected}"
            )
        return outcome

    def list_files(
        self,
        source_paths: list[str],
        *,
        on_source: Callable[[dict[str, Any]], None] | None = None,
        on_path: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """List the paths a backup would consider, writing nothing.

        Returns ``{sources, total, errors}`` where each source entry holds
        ``source``, the ``include_files``/``exclude_files`` in effect, the
        selected ``paths`` and its own ``errors``.  ``on_source`` fires with
        that entry before the source is scanned and ``on_path`` for each path
        as it is found, so a caller can show progress instead of waiting for a
        large scan to finish.  Backends that can drive their tool's dry run
        override this; the default reports that the method cannot list files.
        """
        method = getattr(self, "METHOD_NAME", "unknown")
        return {
            "sources": [],
            "total": 0,
            "errors": [f"Listing files is not supported for the {method} method."],
        }

    @abstractmethod
    def backup(
        self,
        source_paths: list[str],
        *,
        dry_run: bool = False,
        extra_args: dict[str, Any] | None = None,
        scroll_lines: int = 0,
    ) -> dict[str, Any]:
        """Back up the given source paths.

        Returns a dict with summary information (files_copied, bytes_copied, etc.).
        """
        ...

    @abstractmethod
    def restore(
        self,
        restore_path: str,
        path_within_backup: str | None = None,
        *,
        dry_run: bool = False,
        extra_args: dict[str, Any] | None = None,
        scroll_lines: int = 0,
    ) -> dict[str, Any]:
        """Restore files from the backup into ``restore_path``.

        ``path_within_backup`` optionally narrows the restore to a
        subpath within the backup; its files are restored into
        ``RESTORE_DIR/<final component of path_within_backup>``.
        Must never delete files in the restore destination.

        Returns a dict with files_restored, bytes_restored, etc.
        """
        ...

    @abstractmethod
    def status(
        self,
        *,
        extra_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return status information about the backup destination.

        Returns a dict with destination, method, source_paths, dest_path,
        dest_exists, and last_backup details.
        """
        ...
