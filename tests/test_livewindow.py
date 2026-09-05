"""Tests for the LiveWindow status line (TTY features exercised via PTY)."""

from __future__ import annotations

import fcntl
import os
import pty
import struct
import subprocess
import sys
import termios


def _run_with_pty(code: str) -> str:
    """Run ``code`` in a subprocess whose stdout is a sized PTY."""
    master, slave = pty.openpty()
    try:
        # Give the PTY a real size so LiveWindow activates.
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=slave,
            stderr=slave,
            stdin=subprocess.DEVNULL,
            close_fds=True,
        )
        os.close(slave)
        chunks: list[bytes] = []
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        proc.wait()
        return b"".join(chunks).decode("utf-8", errors="replace")
    finally:
        os.close(master)


def test_clean_row_strips_ansi_and_emulates_backspace() -> None:
    from bu.backends.base import LiveWindow

    assert LiveWindow._clean_row("\x1b[1;33mhello\x1b[0m") == "hello"
    # Backspace erases the previous character, like a real terminal.
    assert LiveWindow._clean_row("abc\b\bXY") == "aXY"
    assert LiveWindow._clean_row("") == ""
    assert LiveWindow._clean_row("\x1b[Kprogress") == "progress"


def test_status_line_shows_elapsed_time_and_context() -> None:
    out = _run_with_pty(
        "import sys\n"
        "from bu.backends.base import LiveWindow\n"
        "w = LiveWindow(4, status_interval=0.05, title='test')\n"
        "w.__enter__()\n"
        "w.write('some output row\\n')\n"
        "w.set_context('/Users/paul/Documents')\n"
        "w.write('second output row\\n')\n"
        "w.__exit__(None, None, None)\n"
    )
    assert "some output row" in out
    assert "/Users/paul/Documents" in out
    assert "[00:0" in out  # elapsed-time prefix on the status line


def test_status_context_is_shown_without_output_rows() -> None:
    out = _run_with_pty(
        "from bu.backends.base import LiveWindow\n"
        "w = LiveWindow(3, status_interval=0.05, title='t')\n"
        "w.__enter__()\n"
        "w.set_context('/source/path')\n"
        "w.__exit__(None, None, None)\n"
    )
    assert "/source/path" in out
    assert "[00:0" in out


def test_rows_cap_at_width_minus_one_and_end_with_crlf() -> None:
    out = _run_with_pty(
        "from bu.backends.base import LiveWindow\n"
        "w = LiveWindow(2, status_interval=0.05, title='t')\n"
        "w.__enter__()\n"
        "w.write('A' * 100 + '\\n')\n"
        "w.write('B' * 40 + '\\n')\n"
        "w.__exit__(None, None, None)\n"
    )
    assert "\x1b[?7l" in out   # auto-wrap disabled while the window is active
    assert "\x1b[?7h" in out   # restored on exit
    assert "A" * 100 not in out
    # A 100-char row is capped at width-1 (79 on the 80-col PTY) and
    # terminated with \r\n. The PTY's ONLCR doubles the \r on the wire
    # (\r\n → \r\r\n); a bare \n at the last column would show as a single \r.
    assert "A" * 79 + "\r\r\n" in out
