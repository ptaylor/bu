"""Vanished source files (rsync exit 24) are warnings, not failures.

A file can disappear between rsync's file-list scan and the transfer — cloud
clients rewrite files constantly, and a rename or move does it too.  The new
snapshot simply omits it and the previous snapshot still holds it, so failing
the whole run (and blocking the next one behind `bu backup-restart`) is
heavy-handed.  Real partial-transfer errors must still fail.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bu.backends import local
from bu.backends.local import RsyncMethod, parse_rsync_size


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point bu's config/log/state dirs at tmp_path (AGENTS.md convention)."""
    monkeypatch.setenv("BU_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("BU_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path


def _use_stub(env: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> Path:
    """Install a fake rsync that ignores its args and runs ``body``."""
    stub_dir = env / "bin"
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub = stub_dir / "rsync"
    stub.write_text("#!/bin/sh\n" + body)
    os.chmod(stub, 0o755)
    monkeypatch.setenv("BU_RSYNC", str(stub))
    monkeypatch.setattr(local, "_RSYNC_CACHE", None)
    return stub


def _method(dest: Path, src: Path) -> RsyncMethod:
    return RsyncMethod({
        "destination": str(dest),
        "_name": "test",
        "_source_paths": [str(src)],
        "_exclude_files": [],
        "_source_includes": [[]],
        "_source_excludes": [None],
        "_restore_test_dirs": [],
    })


def _source(env: Path) -> tuple[Path, Path]:
    src = env / "src"
    src.mkdir()
    (src / "a.txt").write_text("x\n")
    dest = env / "dest"
    dest.mkdir()
    return src, dest


def _heredoc(tag: str, text: str, fd: str = "") -> str:
    return f"cat <<'{tag}'{fd}\n{text}{tag}\n"


STATS = (
    "Number of regular files transferred: 3\n"
    "total size is 255.81G  speedup is 19.99\n"
)
VANISHED = (
    'file has vanished: "/src/a.txt"\n'
    "rsync warning: some files vanished before they could be transferred "
    "(code 24) at main.c(1394) [sender=3.5.0]\n"
)
PARTIAL = (
    'rsync: opendir "/src/locked" failed: Permission denied (13)\n'
    "rsync error: some files/attrs were not transferred (see previous errors) "
    "(code 23) at main.c(1394) [sender=3.5.0]\n"
)


@pytest.mark.parametrize(("text", "expected"), [
    ("255.81G", 274_673_895_997),
    ("828.04M", 868_262_871),
    ("2.17K", 2_222),
    ("1.5K", 1_536),
    ("44", 44),
    ("1,234", 1_234),
    ("0", 0),
    ("", 0),
    ("junk", 0),
])
def test_parse_rsync_size(text: str, expected: int) -> None:
    """``--stats`` runs with ``-h``, so the unit suffix must be applied."""
    assert parse_rsync_size(text) == expected


def test_vanished_files_warn_but_do_not_fail_the_run(
    env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    src, dest = _source(env)
    _use_stub(
        env, monkeypatch,
        _heredoc("OUT", STATS) + _heredoc("ERR", VANISHED, " >&2") + "exit 24\n",
    )

    result = _method(dest, src).backup([str(src)], scroll_lines=0)

    assert result["errors"] == []
    # Surfaced as notes (shown by `bu backup`) rather than swallowed.
    assert any("vanished" in note for note in result["notes"])

    status = json.loads((dest / "bu-test-status.txt").read_text())
    assert status["state"] == "completed"
    assert any("vanished" in warn for warn in status["warnings"])
    # The size suffix is applied, so the figure is a real byte count.
    assert status["bytes_copied"] == 274_673_895_997
    assert result["bytes_copied"] == 274_673_895_997


def test_partial_transfer_still_fails_the_run(
    env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    src, dest = _source(env)
    _use_stub(
        env, monkeypatch,
        _heredoc("OUT", STATS) + _heredoc("ERR", PARTIAL, " >&2") + "exit 23\n",
    )

    result = _method(dest, src).backup([str(src)], scroll_lines=0)

    assert result["errors"], "exit 23 must stay fatal"
    assert "Permission denied" in result["errors"][0]
    status = json.loads((dest / "bu-test-status.txt").read_text())
    assert status["state"] == "error"


def test_listing_tolerates_vanished_paths(
    env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    src, dest = _source(env)
    listing = "./\na.txt\n"
    _use_stub(
        env, monkeypatch,
        _heredoc("OUT", listing) + _heredoc("ERR", VANISHED, " >&2") + "exit 24\n",
    )

    result = _method(dest, src).list_files([str(src)])

    assert result["errors"] == []
    assert result["total"] == 2
    assert result["sources"][0]["paths"] == [".", "a.txt"]
