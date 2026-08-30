"""Abstract base class for all backup backends."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Self

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
            self._width = shutil.get_terminal_size().columns
        except OSError:
            return self
        if self._width < 20:
            return self
        # Title bar above the window: "── Title ──────…"
        rule = "─" * max(0, self._width - len(self.title) - 3)
        sys.stdout.write(f"\033[1;33m─ {self.title} {rule}\033[0m\r\n")
        # Reserve window + dividing rule + status rows, then move cursor back
        sys.stdout.write("\n" * (self.lines + 2))
        sys.stdout.write(f"\033[{self.lines + 2}A")
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
        # Always paint exactly `lines` output rows (blank-filling the rest)
        for i in range(self.lines):
            sys.stdout.write("\033[2K")       # erase whole line
            if i < len(self._buf):
                sys.stdout.write(self._colour_row(self._buf[i][: self._width - 1]))
            sys.stdout.write("\n")
        self._painted = self.lines
        # Yellow dividing rule above the status line, matching the title bar
        sys.stdout.write("\033[2K")
        sys.stdout.write(f"\033[1;33m{'─' * self._width}\033[0m")
        sys.stdout.write("\n")
        # Status line below the window — throttled to update infrequently
        now = time.monotonic()
        if self._last_status_paint < 0 or now - self._last_status_paint >= self.status_interval:
            elapsed = now - self._start
            status = f"[{int(elapsed) // 60:02d}:{int(elapsed) % 60:02d}] {self._context}"
            sys.stdout.write("\033[2K")
            sys.stdout.write(f"\033[1;36m{status[: self._width]}\033[0m")
            self._last_status_paint = now
        sys.stdout.flush()


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
