"""Tests for backup status gating, backup-restart, and backup-remove."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bu.actions import action_backup, action_backup_remove, action_restore, format_status
from bu.backends.local import RsyncMethod
from bu.backends.snapshot import SnapshotMethod
from bu.config import Config


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point bu's config/log/state dirs at tmp_path (AGENTS.md convention)."""
    monkeypatch.setenv("BU_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("BU_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path


def make_rsync(dest: Path, src: Path) -> RsyncMethod:
    return RsyncMethod({
        "destination": str(dest),
        "_name": "test",
        "_source_paths": [str(src)],
        "_exclude_files": [],
        "_source_includes": [],
        "_source_excludes": [],
    })


def test_backup_block_reason_states(env: Path) -> None:
    src = env / "src"
    src.mkdir()
    (src / "a.txt").write_text("a\n")
    dest = env / "dest"
    dest.mkdir()
    m = make_rsync(dest, src)
    status = dest / "bu-test-status.txt"

    # No status file → allowed.
    assert m.backup_block_reason() is None
    status.write_text(json.dumps({"method": "rsync", "state": "completed"}))
    assert m.backup_block_reason() is None
    status.write_text(json.dumps({"method": "rsync", "state": "started"}))
    assert "still be running" in m.backup_block_reason()
    status.write_text(json.dumps({"method": "rsync", "state": "error"}))
    assert "did not complete" in m.backup_block_reason()


def test_action_backup_blocks_on_failed_status(env: Path) -> None:
    cfg_dir = env / "config"
    cfg_dir.mkdir()
    src = env / "src"
    src.mkdir()
    (src / "a.txt").write_text("a\n")
    dest = env / "dest"
    dest.mkdir()
    (dest / "bu-test-status.txt").write_text(
        json.dumps({"method": "rsync", "state": "error"})
    )
    (cfg_dir / "test.toml").write_text(
        f'method = "rsync"\nsource_paths = ["{src}"]\ndestination = "{dest}"\n'
    )
    d = Config(cfg_dir).get("test")

    result = action_backup(d, scroll_lines=0)
    assert result["errors"]
    assert "did not complete" in result["errors"][0]
    assert any("bu backup-restart test" in e for e in result["errors"])
    # rsync never ran — nothing was written into the destination.
    assert not (dest / "src").exists()


def test_action_backup_restart_runs_after_reset(env: Path) -> None:
    cfg_dir = env / "config"
    cfg_dir.mkdir()
    src = env / "src"
    src.mkdir()
    (src / "a.txt").write_text("a\n")
    dest = env / "dest"
    dest.mkdir()
    (dest / "bu-test-status.txt").write_text(
        json.dumps({"method": "rsync", "state": "error"})
    )
    (cfg_dir / "test.toml").write_text(
        f'method = "rsync"\nsource_paths = ["{src}"]\ndestination = "{dest}"\n'
    )
    d = Config(cfg_dir).get("test")

    result = action_backup(d, extra_args={"restart": True}, scroll_lines=0)
    assert not result["errors"]
    assert (dest / "src" / "a.txt").exists()
    status = json.loads((dest / "bu-test-status.txt").read_text())
    assert status["state"] == "completed"


def test_snapshot_restart_reuses_same_snapshot_dir(env: Path) -> None:
    src = env / "src"
    (src / "docs").mkdir(parents=True)
    t0 = 1_700_000_000.0
    a = src / "docs" / "a.txt"
    a.write_text("v1\n")
    os.utime(a, (t0, t0))
    b = src / "docs" / "b.txt"
    b.write_text("b\n")
    os.utime(b, (t0, t0))
    dest = env / "dest"
    dest.mkdir()

    m = SnapshotMethod({
        "destination": str(dest),
        "_name": "test",
        "_source_paths": [str(src)],
        "_exclude_files": [],
        "_source_includes": [],
        "_source_excludes": [],
    })
    r1 = m.backup([str(src)], scroll_lines=0)
    assert not r1["errors"]
    ts1 = r1["snapshot"]

    # Simulate a failed second run: a partial snapshot dir + error status.
    a.write_text("v2\n")
    os.utime(a, (t0 + 60, t0 + 60))
    ts2 = "2020-01-01-00.00.00"
    partial = dest / ts2
    (partial / "src" / "docs").mkdir(parents=True)
    (partial / "src" / "partial.txt").write_text("p\n")
    (dest / "bu-test-status.txt").write_text(
        json.dumps({"method": "snapshot", "state": "error", "snapshot": ts2})
    )

    r2 = m.backup([str(src)], extra_args={"restart": True}, scroll_lines=0)
    assert not r2["errors"]
    assert r2["snapshot"] == ts2
    snap = dest / ts2
    assert (snap / "src" / "docs" / "a.txt").read_text() == "v2\n"
    # The stray partial file was cleaned by --delete within the resume dir.
    assert not (snap / "src" / "partial.txt").exists()
    # Changed file got a fresh inode; unchanged file was hard-linked from
    # the PREVIOUS snapshot (the resume dir is excluded as link-dest base).
    assert (dest / ts1 / "src" / "docs" / "a.txt").stat().st_ino != (
        snap / "src" / "docs" / "a.txt"
    ).stat().st_ino
    assert (dest / ts1 / "src" / "docs" / "b.txt").stat().st_ino == (
        snap / "src" / "docs" / "b.txt"
    ).stat().st_ino
    status = json.loads((dest / "bu-test-status.txt").read_text())
    assert status["state"] == "completed"
    assert status["snapshot"] == ts2


def test_snapshot_remove_incomplete(env: Path) -> None:
    src = env / "src"
    src.mkdir()
    dest = env / "dest"
    dest.mkdir()
    m = SnapshotMethod({
        "destination": str(dest),
        "_name": "test",
        "_source_paths": [str(src)],
        "_exclude_files": [],
        "_source_includes": [],
        "_source_excludes": [],
    })

    ts = "2020-01-01-00.00.00"
    snap = dest / ts
    (snap / "junk").mkdir(parents=True)
    (snap / "junk" / "f.txt").write_text("x\n")
    status = dest / "bu-test-status.txt"
    status.write_text(json.dumps({"method": "snapshot", "state": "error", "snapshot": ts}))

    assert m.incomplete_snapshot_info() == {"snapshot": ts, "dir": str(snap)}
    res = m.remove_incomplete_snapshot()
    assert res["ok"]
    assert not snap.exists()
    assert not status.exists()

    # A completed snapshot is never offered for removal.
    snap.mkdir()
    status.write_text(json.dumps({"method": "snapshot", "state": "completed", "snapshot": ts}))
    assert m.incomplete_snapshot_info() is None


def test_backup_restart_refuses_when_completed(env: Path) -> None:
    cfg_dir = env / "config"
    cfg_dir.mkdir()
    src = env / "src"
    src.mkdir()
    (src / "a.txt").write_text("a\n")
    dest = env / "dest"
    dest.mkdir()
    (dest / "bu-test-status.txt").write_text(
        json.dumps({"method": "rsync", "state": "completed"})
    )
    (cfg_dir / "test.toml").write_text(
        f'method = "rsync"\nsource_paths = ["{src}"]\ndestination = "{dest}"\n'
    )
    d = Config(cfg_dir).get("test")

    result = action_backup(d, extra_args={"restart": True}, scroll_lines=0)
    assert result["errors"]
    assert "already completed" in result["errors"][0]
    assert not (dest / "src").exists()


def test_backup_restart_refuses_without_status(env: Path) -> None:
    cfg_dir = env / "config"
    cfg_dir.mkdir()
    src = env / "src"
    src.mkdir()
    (src / "a.txt").write_text("a\n")
    dest = env / "dest"
    dest.mkdir()
    (cfg_dir / "test.toml").write_text(
        f'method = "rsync"\nsource_paths = ["{src}"]\ndestination = "{dest}"\n'
    )
    d = Config(cfg_dir).get("test")

    result = action_backup(d, extra_args={"restart": True}, scroll_lines=0)
    assert result["errors"]
    assert "No previous backup state" in result["errors"][0]
    assert not (dest / "src").exists()


def test_backup_remove_refuses_when_completed(env: Path) -> None:
    cfg_dir = env / "config"
    cfg_dir.mkdir()
    src = env / "src"
    src.mkdir()
    dest = env / "dest"
    dest.mkdir()
    (dest / "bu-test-status.txt").write_text(
        json.dumps({
            "method": "snapshot",
            "state": "completed",
            "snapshot": "2020-01-01-00.00.00",
        })
    )
    (cfg_dir / "test.toml").write_text(
        f'method = "snapshot"\nsource_paths = ["{src}"]\ndestination = "{dest}"\n'
    )
    d = Config(cfg_dir).get("test")

    info = action_backup_remove(d)
    assert not info["ok"]
    assert "nothing to remove" in info["errors"][0]


def test_action_restore_blocked_when_not_completed(env: Path) -> None:
    cfg_dir = env / "config"
    cfg_dir.mkdir()
    src = env / "src"
    src.mkdir()
    (src / "a.txt").write_text("a\n")
    dest = env / "dest"
    dest.mkdir()
    out = env / "out"
    out.mkdir()
    (dest / "bu-test-status.txt").write_text(
        json.dumps({"method": "rsync", "state": "error"})
    )
    (cfg_dir / "test.toml").write_text(
        f'method = "rsync"\nsource_paths = ["{src}"]\ndestination = "{dest}"\n'
    )
    d = Config(cfg_dir).get("test")

    result = action_restore(d, str(out), scroll_lines=0)
    assert result["errors"]
    assert "did not complete" in result["errors"][0]
    assert "blocked until the backup completes" in result["errors"][1]
    assert list(out.iterdir()) == []


def test_action_restore_runs_when_completed(env: Path) -> None:
    cfg_dir = env / "config"
    cfg_dir.mkdir()
    src = env / "src"
    src.mkdir()
    (src / "a.txt").write_text("a\n")
    dest = env / "dest"
    dest.mkdir()
    out = env / "out"
    out.mkdir()
    (cfg_dir / "test.toml").write_text(
        f'method = "rsync"\nsource_paths = ["{src}"]\ndestination = "{dest}"\n'
    )
    d = Config(cfg_dir).get("test")

    # A completed run first, then restore works.
    assert not action_backup(d, scroll_lines=0)["errors"]
    result = action_restore(d, str(out), scroll_lines=0)
    assert not result["errors"]
    assert (out / "src" / "a.txt").exists()


def test_snapshot_restart_resumes_newest_dir_without_snapshot_key(env: Path) -> None:
    src = env / "src"
    src.mkdir()
    (src / "a.txt").write_text("a\n")
    dest = env / "dest"
    dest.mkdir()
    m = SnapshotMethod({
        "destination": str(dest),
        "_name": "test",
        "_source_paths": [str(src)],
        "_exclude_files": [],
        "_source_includes": [],
        "_source_excludes": [],
    })

    # A previous completed snapshot.
    old = dest / "2019-01-01-00.00.00"
    (old / "src").mkdir(parents=True)
    (old / "src" / "a.txt").write_text("old\n")

    # Interrupted run: newest dir exists, but the 'started' status has no
    # snapshot key (the pre-fix format).
    ts = "2020-01-01-00.00.00"
    partial = dest / ts
    (partial / "src").mkdir(parents=True)
    (dest / "bu-test-status.txt").write_text(
        json.dumps({"method": "snapshot", "state": "started"})
    )

    r = m.backup([str(src)], extra_args={"restart": True}, scroll_lines=0)
    assert not r["errors"]
    assert r["snapshot"] == ts
    assert (partial / "src" / "a.txt").read_text() == "a\n"
    status = json.loads((dest / "bu-test-status.txt").read_text())
    assert status["state"] == "completed"
    assert status["snapshot"] == ts


def test_format_status_shows_remediation_hint() -> None:
    out = format_status({
        "name": "docs",
        "method": "snapshot",
        "sources": [],
        "dest_path": "/d",
        "dest_exists": True,
        "last_backup": {"state": "error", "timestamp": ""},
    })
    assert "Next steps" in out
    assert "bu backup-restart docs" in out
    assert "bu backup-remove docs" in out

    # A running backup shows a friendly note plus what to do if it's stuck.
    out2 = format_status({
        "name": "docs",
        "method": "rsync",
        "sources": [],
        "dest_path": "/d",
        "dest_exists": True,
        "last_backup": {"state": "started", "timestamp": ""},
    })
    assert "backup is running" in out2
    assert "bu backup-restart docs" in out2
    assert "backup-remove" not in out2

    out3 = format_status({
        "name": "docs",
        "method": "snapshot",
        "sources": [],
        "dest_path": "/d",
        "dest_exists": True,
        "last_backup": {"state": "started", "timestamp": ""},
    })
    assert "bu backup-remove docs" in out3
