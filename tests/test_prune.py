"""Tests for `bu prune` — the snapshot retention plan (listing only).

The plan is a pure function of the snapshot names plus a ``now``, so the tests
inject a fixed clock instead of patching the system one.  Pruning deletes
nothing in this version, and the tests assert that too.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bu.actions import action_prune, format_prune
from bu.backends.snapshot import SnapshotMethod
from bu.cli import main
from bu.config import DestinationConfig

NOW = datetime.datetime(2026, 9, 27, 12, 0, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point bu's config/log/state dirs at tmp_path (AGENTS.md convention)."""
    monkeypatch.setenv("BU_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("BU_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path


def snapshot(dest: Path, when: datetime.datetime, suffix: str = "") -> str:
    """Create a snapshot directory named for ``when`` and return its name."""
    name = when.strftime("%Y-%m-%d-%H.%M.%S") + suffix
    (dest / name).mkdir(parents=True, exist_ok=True)
    return name


def ago(days: int, hour: int = 12) -> datetime.datetime:
    return (NOW - datetime.timedelta(days=days)).replace(hour=hour)


def make_method(dest: Path, src: Path) -> SnapshotMethod:
    return SnapshotMethod({
        "destination": str(dest),
        "_name": "test",
        "_source_paths": [str(src)],
        "_exclude_files": [],
        "_source_includes": [[]],
        "_source_excludes": [None],
        "_restore_test_dirs": [],
    })


def setup_dest(env: Path) -> tuple[Path, Path, SnapshotMethod]:
    src = env / "src"
    src.mkdir()
    dest = env / "dest"
    dest.mkdir()
    return src, dest, make_method(dest, src)


def names(entries: list[dict]) -> list[str]:
    return [entry["name"] for entry in entries]


@pytest.mark.parametrize(("age_days", "kind"), [
    (-5, "today"),   # future-dated (clock skew) is never pruned for being new
    (0, "today"),
    (1, "daily"),
    (7, "daily"),
    (8, "weekly"),
    (31, "weekly"),
    (32, "monthly"),
    (365, "monthly"),
    (366, "yearly"),
    (2000, "yearly"),
])
def test_bucket_for_every_boundary(age_days: int, kind: str) -> None:
    ts = NOW - datetime.timedelta(days=age_days)
    assert SnapshotMethod._bucket_for(ts, NOW)[0] == kind


def test_today_keeps_every_snapshot(env: Path) -> None:
    _src, dest, method = setup_dest(env)
    for hour in (8, 10, 12):
        snapshot(dest, NOW.replace(hour=hour))

    plan = method.prune_plan(now=NOW)

    assert len(plan["keep"]) == 3
    assert plan["remove"] == []
    assert {entry["reason"] for entry in plan["keep"]} == {"today"}


def test_one_per_day_within_the_week(env: Path) -> None:
    _src, dest, method = setup_dest(env)
    for hour in (8, 10, 12):
        snapshot(dest, ago(2, hour))
    older = snapshot(dest, ago(3))

    plan = method.prune_plan(now=NOW)

    assert names(plan["keep"]) == [older, ago(2, 12).strftime("%Y-%m-%d-%H.%M.%S")]
    assert names(plan["remove"]) == [
        ago(2, 8).strftime("%Y-%m-%d-%H.%M.%S"),
        ago(2, 10).strftime("%Y-%m-%d-%H.%M.%S"),
    ]
    assert all("superseded by" in entry["reason"] for entry in plan["remove"])
    assert plan["remove"][0]["superseded_by"] == ago(2, 12).strftime("%Y-%m-%d-%H.%M.%S")


def test_one_per_week_within_the_month(env: Path) -> None:
    _src, dest, method = setup_dest(env)
    snapshot(dest, ago(10, 9))
    kept_week = snapshot(dest, ago(10, 15))
    snapshot(dest, ago(20, 9))
    kept_older = snapshot(dest, ago(20, 15))

    plan = method.prune_plan(now=NOW)

    assert names(plan["keep"]) == [kept_older, kept_week]
    assert len(plan["remove"]) == 2
    assert all(entry["bucket"].startswith("week ") for entry in plan["remove"])


def test_one_per_month_within_the_year(env: Path) -> None:
    _src, dest, method = setup_dest(env)
    snapshot(dest, ago(45))          # 2026-08-13
    kept_aug = snapshot(dest, ago(40))   # 2026-08-18, same calendar month
    kept_jul = snapshot(dest, ago(70))   # 2026-07-19, its own month

    plan = method.prune_plan(now=NOW)

    assert names(plan["keep"]) == [kept_jul, kept_aug]
    assert names(plan["remove"]) == [ago(45).strftime("%Y-%m-%d-%H.%M.%S")]
    assert plan["remove"][0]["bucket"] == "month 2026-08"


def test_one_per_year_beyond_a_year(env: Path) -> None:
    _src, dest, method = setup_dest(env)
    snapshot(dest, ago(500))         # 2025-05-15
    kept_2025 = snapshot(dest, ago(400))   # 2025-08-23
    kept_2024 = snapshot(dest, ago(800))   # 2024-07-20

    plan = method.prune_plan(now=NOW)

    assert names(plan["keep"]) == [kept_2024, kept_2025]
    assert names(plan["remove"]) == [ago(500).strftime("%Y-%m-%d-%H.%M.%S")]
    assert plan["remove"][0]["bucket"] == "year 2025"


def test_nothing_is_deleted(env: Path) -> None:
    """The whole point of this version: the plan is a plan, not an action."""
    _src, dest, method = setup_dest(env)
    created = [snapshot(dest, ago(45)), snapshot(dest, ago(40)), snapshot(dest, ago(2))]
    before = sorted(p.name for p in dest.iterdir())

    plan = method.prune_plan(now=NOW)
    action_prune(DestinationConfig("test", {
        "method": "snapshot",
        "source_paths": [str(env / "src")],
        "destination": str(dest),
    }))

    assert sorted(p.name for p in dest.iterdir()) == before
    assert set(before) == set(created)
    assert plan["ok"] is True


def test_plan_writes_nothing(env: Path) -> None:
    _src, dest, method = setup_dest(env)
    snapshot(dest, ago(45))

    method.prune_plan(now=NOW)

    assert not (dest / "bu-test-status.txt").exists()
    assert not (env / "state").exists()
    assert not (env / "logs").exists()


def test_same_second_suffix_keeps_the_later_name(env: Path) -> None:
    _src, dest, method = setup_dest(env)
    when = ago(45)
    first = snapshot(dest, when)
    second = snapshot(dest, when, suffix="-2")

    plan = method.prune_plan(now=NOW)

    assert names(plan["keep"]) == [second]
    assert names(plan["remove"]) == [first]


def test_incomplete_run_directory_is_never_a_candidate(env: Path) -> None:
    _src, dest, method = setup_dest(env)
    old = snapshot(dest, ago(500))       # would otherwise be removed
    snapshot(dest, ago(400))             # the bucket's newest
    (dest / "bu-test-status.txt").write_text(
        json.dumps({"state": "error", "snapshot": old})
    )

    plan = method.prune_plan(now=NOW)

    assert plan["protected"] == old
    assert old in names(plan["keep"])
    assert old not in names(plan["remove"])
    reason = next(e["reason"] for e in plan["keep"] if e["name"] == old)
    assert "did not complete" in reason and "backup-remove" in reason


def test_completed_status_protects_nothing_extra(env: Path) -> None:
    _src, dest, method = setup_dest(env)
    oldest = snapshot(dest, ago(500))
    newest = snapshot(dest, ago(400))
    (dest / "bu-test-status.txt").write_text(
        json.dumps({"state": "completed", "snapshot": newest})
    )

    plan = method.prune_plan(now=NOW)

    assert plan["protected"] is None
    assert names(plan["remove"]) == [oldest]


def test_empty_destination_is_not_an_error(env: Path) -> None:
    _src, _dest, method = setup_dest(env)

    plan = method.prune_plan(now=NOW)

    assert plan["ok"] is True
    assert plan["snapshots"] == 0
    assert plan["keep"] == [] and plan["remove"] == []


def test_missing_destination_is_an_error(env: Path) -> None:
    src = env / "src"
    src.mkdir()
    result = action_prune(DestinationConfig("test", {
        "method": "snapshot",
        "source_paths": [str(src)],
        "destination": str(env / "not-mounted"),
    }))

    assert result["ok"] is False
    assert "Destination not found" in result["errors"][0]
    assert "mounted" in result["errors"][0]


def test_non_snapshot_method_reports_clearly(env: Path) -> None:
    src = env / "src"
    src.mkdir()
    dest = env / "dest"
    dest.mkdir()
    result = action_prune(DestinationConfig("test", {
        "method": "rsync",
        "source_paths": [str(src)],
        "destination": str(dest),
    }))

    assert result["ok"] is False
    assert result["errors"] == [
        "prune only applies to snapshot destinations ('test' uses rsync)"
    ]
    assert "Error:" in format_prune(result)


def test_format_prune_shows_both_sections(env: Path) -> None:
    _src, dest, method = setup_dest(env)
    snapshot(dest, ago(45))
    snapshot(dest, ago(40))

    output = format_prune(method.prune_plan(now=NOW))

    assert "Keep (1)" in output
    assert "Remove (1)" in output
    assert "1 of 2 snapshot(s) would be removed" in output
    assert "nothing was deleted" in output


def test_cli_lists_the_plan_without_deleting(env: Path) -> None:
    src = env / "src"
    src.mkdir()
    dest = env / "dest"
    dest.mkdir()
    snapshot(dest, NOW - datetime.timedelta(days=45))
    snapshot(dest, NOW - datetime.timedelta(days=40))
    cfg_dir = env / "config"
    cfg_dir.mkdir()
    (cfg_dir / "t.toml").write_text(
        f'method = "snapshot"\n'
        f'source_paths = ["{src}"]\n'
        f'destination = "{dest}"\n'
    )
    before = sorted(p.name for p in dest.iterdir())

    result = CliRunner().invoke(main, ["prune", "t"])

    assert result.exit_code == 0, result.output
    assert "Keep (1)" in result.output
    assert "Remove (1)" in result.output
    assert "nothing was deleted" in result.output
    # The CLI really is the read-only half.
    assert sorted(p.name for p in dest.iterdir()) == before


def test_cli_rejects_a_non_snapshot_destination(env: Path) -> None:
    src = env / "src"
    src.mkdir()
    dest = env / "dest"
    dest.mkdir()
    cfg_dir = env / "config"
    cfg_dir.mkdir()
    (cfg_dir / "t.toml").write_text(
        f'method = "rsync"\n'
        f'source_paths = ["{src}"]\n'
        f'destination = "{dest}"\n'
    )

    result = CliRunner().invoke(main, ["prune", "t"])

    assert result.exit_code == 1
    assert "prune only applies to snapshot destinations" in result.output
