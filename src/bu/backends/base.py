"""Abstract base class for all backup backends."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


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
    A status line below the window shows the latest output row plus
    elapsed time, repainted at most every ``status_interval`` seconds.
    A yellow ``─`` dividing rule sits between the window rows and the
    status line, matching the title bar.
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
        self._status = ""
        self._painted = 0
        self._width = 80
        self._start = 0.0
        self._last_status_paint = -1.0

    # -- context management ---------------------------------------------

    def __enter__(self) -> "LiveWindow":
        if self.lines <= 0 or not sys.stdout.isatty():
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
        return self

    def __exit__(self, *exc) -> bool:
        if self.active:
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
            # with \r, and each update becomes its own display row.
            rows = [r for r in re.split(r"[\r\n]", text) if r]
            if rows:
                self._buf.extend(rows)
                if len(self._buf) > self.lines:
                    del self._buf[: len(self._buf) - self.lines]
                self._status = self._buf[-1]
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
        if row.startswith("Transfer starting") or row.startswith("Number of"):
            return f"\033[32m{row}\033[0m"          # green
        return row

    def _redraw(self) -> None:
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
            status = f"[{int(elapsed) // 60:02d}:{int(elapsed) % 60:02d}] {self._status}"
            sys.stdout.write("\033[2K")
            sys.stdout.write(f"\033[1;36m{status[: self._width]}\033[0m")
            self._last_status_paint = now
        sys.stdout.flush()


def run_streaming(
    cmd: list[str],
    env: dict[str, str] | None = None,
    scroll_lines: int = 0,
    window: "LiveWindow | None" = None,
    title: str = "Live output",
    silence_stderr: bool = False,
) -> StreamResult:
    """Run a command, streaming its output live to the terminal while capturing.

    stdout/stderr are written to the terminal as they arrive AND collected
    for later logging.  If ``scroll_lines`` > 0 and stdout is a TTY, output
    is confined to a Docker-style live window of that height; otherwise
    output passes through as plain text.

    If ``window`` is supplied, it is used instead of creating one — the
    caller owns its lifecycle (useful for sharing one window across
    several subprocess runs).  ``silence_stderr`` captures stderr without
    writing it anywhere (the caller can re-emit condensed lines later).
    Return a StreamResult with combined output strings.
    """
    owns_window = window is None
    if window is None:
        window = LiveWindow(scroll_lines, title=title)
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

        t1 = threading.Thread(target=_tee, args=(proc.stdout, window, captured["stdout"]))
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
