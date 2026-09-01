"""Tests for rsync binary resolution (BU_RSYNC, Homebrew preference, openrsync)."""

import os
from pathlib import Path

from bu.actions import format_result
from bu.backends import local
from bu.backends.base import StreamResult
from bu.backends.local import (
    RsyncMethod,
    _is_openrsync,
    _openrsync_warning,
    resolve_rsync_binary,
)


def _write_bin(path, first_line):
    """Create a fake rsync executable that prints ``first_line``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho '{first_line}'\n")
    os.chmod(path, 0o755)
    return path


def _clear_cache(monkeypatch):
    monkeypatch.setattr(local, "_RSYNC_CACHE", None)
    monkeypatch.delenv("BU_RSYNC", raising=False)


def test_openrsync_detection(tmp_path):
    good = _write_bin(tmp_path / "rsync-good", "rsync  version 3.4.1  protocol version 31")
    apple = _write_bin(tmp_path / "rsync-apple", "openrsync: protocol version 29")
    old = _write_bin(tmp_path / "rsync-old", "rsync  version 2.6.9  protocol version 29")
    assert not _is_openrsync(str(good))
    assert _is_openrsync(str(apple))
    assert _is_openrsync(str(old))


def test_bu_rsync_override(tmp_path, monkeypatch):
    _clear_cache(monkeypatch)
    monkeypatch.setenv("BU_RSYNC", str(tmp_path / "my-rsync"))
    assert resolve_rsync_binary() == str(tmp_path / "my-rsync")


def test_prefers_full_rsync_from_path(tmp_path, monkeypatch):
    _clear_cache(monkeypatch)
    good = _write_bin(tmp_path / "good" / "rsync", "rsync  version 3.2.7  protocol version 31")
    monkeypatch.setattr(local.shutil, "which", lambda _name: str(good))
    assert resolve_rsync_binary() == str(good)


def test_falls_back_to_candidate_when_path_is_openrsync(tmp_path, monkeypatch):
    _clear_cache(monkeypatch)
    apple = _write_bin(tmp_path / "apple" / "rsync", "openrsync: protocol version 29")
    brew = _write_bin(tmp_path / "brew" / "rsync", "rsync  version 3.4.1  protocol version 31")
    monkeypatch.setattr(local.shutil, "which", lambda _name: str(apple))
    monkeypatch.setattr(local, "_RSYNC_CANDIDATES", (str(brew),))
    assert resolve_rsync_binary() == str(brew)


def test_falls_back_to_default_when_no_candidate(tmp_path, monkeypatch):
    _clear_cache(monkeypatch)
    apple = _write_bin(tmp_path / "apple" / "rsync", "openrsync: protocol version 29")
    monkeypatch.setattr(local.shutil, "which", lambda _name: str(apple))
    monkeypatch.setattr(local, "_RSYNC_CANDIDATES", ())
    assert resolve_rsync_binary() == str(apple)


def test_openrsync_warning_mentions_brew(tmp_path):
    apple = _write_bin(tmp_path / "rsync", "openrsync: protocol version 29")
    warning = _openrsync_warning(str(apple))
    assert warning and "brew install rsync" in warning
    good = _write_bin(tmp_path / "rsync3", "rsync  version 3.4.1  protocol version 31")
    assert _openrsync_warning(str(good)) is None


def test_format_result_renders_notes():
    out = format_result({"ok": True, "notes": ["careful now"], "errors": []})
    assert "ℹ careful now" in out
    assert "notes:" not in out


def test_rsync_one_skips_specials(tmp_path, monkeypatch):
    """--no-specials must be passed so socket files can't kill SMB backups."""
    captured: dict[str, list[str]] = {}

    def fake_run_streaming(cmd, **_kwargs):
        captured["cmd"] = list(cmd)
        return StreamResult(0, "", "")

    monkeypatch.setattr(local, "run_streaming", fake_run_streaming)
    src = Path(tmp_path) / "src"
    src.mkdir()
    (src / "a").write_text("x")
    method = RsyncMethod({"destination": str(Path(tmp_path) / "dest")})
    result = method._rsync_one(src, Path(tmp_path) / "dest", False)
    cmd = captured["cmd"]
    assert "--no-specials" in cmd
    assert result["errors"] == []
    assert result["files"] == 0
