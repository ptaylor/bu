"""Tests for the open-file limit bu guarantees its subprocesses.

duplicity 3.x calls ``log.FatalError`` for any full/incremental/restore run
when the soft ``RLIMIT_NOFILE`` is below 1024, and macOS hands 256 to
processes started from Finder, launchd or cron.
"""

from __future__ import annotations

import sys

from bu.backends.base import MIN_OPEN_FILE_LIMIT, ensure_open_file_limit, run_streaming

_PROBE = "import resource; print(resource.getrlimit(resource.RLIMIT_NOFILE)[0])"


def _read_soft_limit_in_child() -> int:
    """Return the soft limit a freshly spawned child process sees."""
    result = run_streaming([sys.executable, "-c", _PROBE], silence_stdout=True, silence_stderr=True)
    return int(result.stdout.strip())


def test_low_soft_limit_is_raised_in_place() -> None:
    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
        assert ensure_open_file_limit() is True
        assert resource.getrlimit(resource.RLIMIT_NOFILE)[0] >= MIN_OPEN_FILE_LIMIT
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_spawned_tools_see_at_least_the_minimum() -> None:
    """A child started by bu inherits a limit duplicity accepts."""
    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
        assert _read_soft_limit_in_child() >= MIN_OPEN_FILE_LIMIT
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_higher_limit_is_left_alone() -> None:
    import resource

    soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft < MIN_OPEN_FILE_LIMIT:  # pragma: no cover - environment dependent
        return
    ensure_open_file_limit()
    assert resource.getrlimit(resource.RLIMIT_NOFILE)[0] == soft
