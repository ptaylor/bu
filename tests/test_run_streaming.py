"""Tests for run_streaming capture/silence flags."""

from __future__ import annotations

import sys


def test_silence_stdout_captures_without_printing(capsys) -> None:
    from bu.backends.base import run_streaming

    res = run_streaming(
        [sys.executable, "-c", "import sys; print('OUT'); sys.stderr.write('ERR')"],
        silence_stdout=True,
        silence_stderr=True,
    )
    assert "OUT" in res.stdout
    assert "ERR" in res.stderr
    captured = capsys.readouterr()
    assert "OUT" not in captured.out
    assert "ERR" not in captured.err


def test_default_streams_output_to_terminal(capsys) -> None:
    from bu.backends.base import run_streaming

    res = run_streaming(
        [sys.executable, "-c", "import sys; print('OUT'); sys.stderr.write('ERR')"],
    )
    assert "OUT" in res.stdout
    assert "ERR" in res.stderr
    captured = capsys.readouterr()
    # Both streams are echoed to the terminal (LiveWindow passthrough writes
    # to stdout when the window is inactive).
    assert "OUT" in captured.out
    assert "ERR" in captured.out
