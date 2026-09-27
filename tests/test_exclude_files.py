"""Tests for additive exclusion files (a source's own + destination defaults)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bu.backends.duplicity import DuplicityMethod
from bu.backends.local import RsyncMethod

BACKENDS = [RsyncMethod, DuplicityMethod]


def _make_files(tmp_path: Path, *names: str) -> list[str]:
    """Create empty filter files and return their paths as strings."""
    paths: list[str] = []
    for name in names:
        path = tmp_path / name
        path.write_text("*.tmp\n")
        paths.append(str(path))
    return paths


def _make_backend(
    cls: type,
    tmp_path: Path,
    own_excludes: list[str] | None,
    defaults: list[str],
) -> object:
    config: dict = {
        "destination": str(tmp_path / "dest"),
        "_name": "test",
        "_source_paths": [str(tmp_path / "src")],
        "_exclude_files": defaults,
        "_source_includes": [[]],
        "_source_excludes": [own_excludes],
    }
    return cls(config)


@pytest.mark.parametrize("backend_cls", BACKENDS)
def test_source_excludes_are_additive(tmp_path: Path, backend_cls: type) -> None:
    """A source's own exclusion files come first, then the destination defaults."""
    global_ex, own = _make_files(tmp_path, "global.txt", "own.txt")
    backend = _make_backend(backend_cls, tmp_path, [own], [global_ex])

    assert backend._source_exclude_files(0) == [own, global_ex]  # type: ignore[attr-defined]
    assert backend._source_filter_files(0)[1] == [own, global_ex]  # type: ignore[attr-defined]


@pytest.mark.parametrize("backend_cls", BACKENDS)
def test_defaults_apply_when_source_has_none(tmp_path: Path, backend_cls: type) -> None:
    global_ex = _make_files(tmp_path, "global.txt")[0]
    backend = _make_backend(backend_cls, tmp_path, None, [global_ex])

    assert backend._source_exclude_files(0) == [global_ex]  # type: ignore[attr-defined]


@pytest.mark.parametrize("backend_cls", BACKENDS)
def test_empty_source_excludes_keep_the_defaults(tmp_path: Path, backend_cls: type) -> None:
    """``exclude = []`` adds nothing — the global exclusions still apply."""
    global_ex = _make_files(tmp_path, "global.txt")[0]
    backend = _make_backend(backend_cls, tmp_path, [], [global_ex])

    assert backend._source_exclude_files(0) == [global_ex]  # type: ignore[attr-defined]


@pytest.mark.parametrize("backend_cls", BACKENDS)
def test_duplicates_and_missing_files(tmp_path: Path, backend_cls: type) -> None:
    """Repeating a default is harmless; missing files never reach the tool."""
    global_ex, own = _make_files(tmp_path, "global.txt", "own.txt")
    missing = str(tmp_path / "nope.txt")
    backend = _make_backend(backend_cls, tmp_path, [own, global_ex, missing], [global_ex, missing])

    assert backend._source_exclude_files(0) == [own, global_ex]  # type: ignore[attr-defined]
    # The status view keeps missing files (so they can be reported as such)
    # but still deduplicates.
    assert backend._source_filter_files(0)[1] == [own, global_ex, missing]  # type: ignore[attr-defined]
