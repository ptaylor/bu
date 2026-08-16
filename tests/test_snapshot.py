"""Tests for the snapshot backup method (rsync --link-dest)."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from bu.actions import _sample_for_method, format_status
from bu.backends import get_backend
from bu.backends.snapshot import SnapshotMethod, check_hardlink_support
from bu.config import Config, ConfigError


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point bu's config/log/state dirs at tmp_path (AGENTS.md convention)."""
    monkeypatch.setenv("BU_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("BU_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path


def make_method(dest: Path, source_paths: list[Path], name: str = "test") -> SnapshotMethod:
    """Build a SnapshotMethod pointing at an existing destination dir."""
    return SnapshotMethod({
        "destination": str(dest),
        "_name": name,
        "_source_paths": [str(p) for p in source_paths],
        "_exclude_files": [],
    })


# rsync's quick check compares size + mtime, so tests pin mtimes explicitly:
# files modified between backups get a later mtime (no sleeps needed).
_T0 = 1_700_000_000.0


def _touch(path: Path, t: float = _T0) -> None:
    os.utime(path, (t, t))


def test_unchanged_files_are_hard_linked(env: Path) -> None:
    src = env / "src"
    (src / "sub").mkdir(parents=True)
    (src / "keep.txt").write_text("same\n")
    (src / "sub" / "change.txt").write_text("v1\n")
    (src / "gone.txt").write_text("gone\n")
    for f in src.rglob("*"):
        _touch(f)
    dest = env / "dest"
    dest.mkdir()

    m = make_method(dest, [src])

    r1 = m.backup([str(src)], scroll_lines=0)
    assert not r1["errors"]
    snap1 = dest / r1["snapshot"]

    (src / "sub" / "change.txt").write_text("v2\n")
    _touch(src / "sub" / "change.txt", _T0 + 60)
    (src / "gone.txt").unlink()
    (src / "new.txt").write_text("new\n")
    _touch(src / "new.txt", _T0 + 60)

    r2 = m.backup([str(src)], scroll_lines=0)
    assert not r2["errors"]
    snap2 = dest / r2["snapshot"]
    assert snap2 != snap1

    # Unchanged file is a hard link shared with the previous snapshot.
    keep1 = snap1 / "src" / "keep.txt"
    keep2 = snap2 / "src" / "keep.txt"
    assert keep1.stat().st_ino == keep2.stat().st_ino
    assert keep1.stat().st_nlink == 2

    # Changed file gets a fresh inode.
    assert (snap1 / "src" / "sub" / "change.txt").stat().st_ino != (
        snap2 / "src" / "sub" / "change.txt").stat().st_ino

    # Deleted/new files appear only in the right snapshots.
    assert (snap1 / "src" / "gone.txt").exists()
    assert not (snap2 / "src" / "gone.txt").exists()
    assert (snap2 / "src" / "new.txt").exists()


def test_backup_aborts_when_hard_links_unsupported(
    env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = env / "src"
    src.mkdir()
    (src / "a.txt").write_text("a\n")
    dest = env / "dest"
    dest.mkdir()

    m = make_method(dest, [src])

    def fake_link(src_: str, dst: str, **kwargs: object) -> None:
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "link", fake_link)

    result = m.backup([str(src)], scroll_lines=0)
    assert result["errors"]
    assert "hard links" in result["errors"][0]
    assert list(dest.iterdir()) == []


def test_hardlink_check_skipped_when_status_file_exists(
    env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = env / "src"
    src.mkdir()
    a = src / "a.txt"
    a.write_text("a\n")
    _touch(a)
    dest = env / "dest"
    dest.mkdir()

    m = make_method(dest, [src])
    assert not m.backup([str(src)], scroll_lines=0)["errors"]

    def fake_link(src_: str, dst: str, **kwargs: object) -> None:
        raise OSError(errno.EPERM, "Operation not permitted")

    # The status file records a previous snapshot backup, so the check
    # must not run again.
    monkeypatch.setattr(os, "link", fake_link)
    r2 = m.backup([str(src)], scroll_lines=0)
    assert not r2["errors"]


def test_hardlink_check_runs_for_foreign_status_file(
    env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = env / "src"
    src.mkdir()
    (src / "a.txt").write_text("a\n")
    dest = env / "dest"
    dest.mkdir()
    (dest / "bu-test-status.txt").write_text('{"method": "rsync", "state": "completed"}')

    m = make_method(dest, [src])

    def fake_link(src_: str, dst: str, **kwargs: object) -> None:
        raise OSError(errno.EPERM, "Operation not permitted")

    # A status file from a different method doesn't prove hard links work.
    monkeypatch.setattr(os, "link", fake_link)
    result = m.backup([str(src)], scroll_lines=0)
    assert result["errors"]
    assert "hard links" in result["errors"][0]


def test_status_reports_snapshots(env: Path) -> None:
    src = env / "src"
    src.mkdir()
    (src / "a.txt").write_text("a\n")
    dest = env / "dest"
    dest.mkdir()

    m = make_method(dest, [src])
    m.backup([str(src)], scroll_lines=0)
    m.backup([str(src)], scroll_lines=0)

    st = m.status()
    assert st["method"] == "snapshot"
    assert st["snapshot_count"] == 2
    assert st["latest_snapshot"] == st["snapshots"][-1]
    assert (dest / "bu-test-status.txt").is_file()
    assert st["last_backup"]["state"] == "completed"


def test_restore_defaults_to_latest_and_path_selects_snapshot(env: Path) -> None:
    src = env / "src"
    src.mkdir()
    a = src / "a.txt"
    a.write_text("v1\n")
    _touch(a)
    dest = env / "dest"
    dest.mkdir()

    m = make_method(dest, [src])
    r1 = m.backup([str(src)], scroll_lines=0)
    assert not r1["errors"]
    ts1 = r1["snapshot"]

    a.write_text("v2\n")
    _touch(a, _T0 + 60)
    r2 = m.backup([str(src)], scroll_lines=0)
    assert not r2["errors"]

    # Default: newest snapshot.
    out1 = env / "out1"
    out1.mkdir()
    res = m.restore(str(out1), scroll_lines=0)
    assert not res["errors"]
    assert (out1 / "src" / "a.txt").read_text() == "v2\n"

    # PATH naming an older timestamp restores that snapshot.
    out2 = env / "out2"
    out2.mkdir()
    res = m.restore(str(out2), ts1, scroll_lines=0)
    assert not res["errors"]
    assert (out2 / "src" / "a.txt").read_text() == "v1\n"

    # A path within the backup restores into RESTORE_DIR/<final component>.
    out3 = env / "out3"
    out3.mkdir()
    res = m.restore(str(out3), "src/a.txt", scroll_lines=0)
    assert not res["errors"]
    assert (out3 / "a.txt").read_text() == "v2\n"

    # Unknown timestamp errors out, listing available snapshots.
    out4 = env / "out4"
    out4.mkdir()
    res = m.restore(str(out4), "1999-01-01-00.00.00", scroll_lines=0)
    assert res["errors"]
    assert "not found" in res["errors"][0]


def test_dry_run_creates_nothing(env: Path) -> None:
    src = env / "src"
    src.mkdir()
    (src / "a.txt").write_text("a\n")
    dest = env / "dest"
    dest.mkdir()

    m = make_method(dest, [src])
    res = m.backup([str(src)], dry_run=True, scroll_lines=0)
    assert not res["errors"]
    assert list(dest.iterdir()) == []


def test_duplicate_source_basenames_get_suffixes(env: Path) -> None:
    a = env / "a" / "docs"
    b = env / "b" / "docs"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    (a / "x.txt").write_text("a\n")
    (b / "x.txt").write_text("b\n")
    dest = env / "dest"
    dest.mkdir()

    m = make_method(dest, [a, b])
    r = m.backup([str(a), str(b)], scroll_lines=0)
    assert not r["errors"]
    snap = dest / r["snapshot"]
    assert (snap / "docs" / "x.txt").read_text() == "a\n"
    assert (snap / "docs_2" / "x.txt").read_text() == "b\n"


def test_hardlink_check_passes_on_normal_filesystem(env: Path) -> None:
    dest = env / "dest"
    dest.mkdir()
    m = make_method(dest, [])
    assert m._check_hardlink_support() == []
    assert check_hardlink_support(dest) == []


def test_module_level_hardlink_check(
    env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    dest = env / "dest"
    dest.mkdir()

    def fake_link(src_: str, dst: str, **kwargs: object) -> None:
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "link", fake_link)
    errors = check_hardlink_support(dest)
    assert errors
    assert "hard links" in errors[0]


def test_registry_and_samples() -> None:
    assert get_backend("snapshot") is SnapshotMethod
    template = _sample_for_method("snapshot", "docs")
    assert 'method = "snapshot"' in template
    assert "history_file" in template


def test_config_accepts_snapshot_method(env: Path) -> None:
    cfg_dir = env / "config"
    cfg_dir.mkdir()
    (cfg_dir / "docs.toml").write_text(
        'method = "snapshot"\nsource_paths = ["~/Documents"]\ndestination = "/mnt/backup"\n'
    )
    cfg = Config(cfg_dir)
    assert "docs" in cfg.list_destinations()
    assert cfg.get("docs").method == "snapshot"


def test_unknown_method_error_mentions_snapshot(env: Path) -> None:
    cfg_dir = env / "config"
    cfg_dir.mkdir()
    (cfg_dir / "bad.toml").write_text(
        'method = "ftp"\nsource_paths = ["/a"]\ndestination = "/b"\n'
    )
    with pytest.raises(ConfigError) as exc:
        Config(cfg_dir)
    assert "snapshot" in str(exc.value)


def test_format_status_lists_snapshots() -> None:
    out = format_status({
        "name": "docs",
        "method": "snapshot",
        "sources": [],
        "dest_path": "/mnt/backup",
        "dest_exists": True,
        "snapshot_count": 2,
        "snapshots": ["2026-08-15-21.09.41", "2026-08-16-10.00.00"],
        "latest_snapshot": "2026-08-16-10.00.00",
        "last_backup": None,
    })
    assert "Snapshots    : 2" in out
    assert "2026-08-16-10.00.00 ← latest" in out
