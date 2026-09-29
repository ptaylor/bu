"""Tests for restore-test markers and the `bu restore-test` command."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from bu.actions import _build_backend, action_restore_test, format_restore_test
from bu.backends import duplicity
from bu.backends.base import on_disk_path
from bu.backends.duplicity import DuplicityMethod
from bu.backends.local import RsyncMethod
from bu.backends.snapshot import SnapshotMethod
from bu.config import Config, DestinationConfig
from bu.filters import last_entry, pattern_matches


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point bu's config/log/state dirs at tmp_path (AGENTS.md convention)."""
    monkeypatch.setenv("BU_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("BU_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path


def make_method(
    backend_cls: type,
    dest: Path,
    sources: list[Path],
    *,
    name: str = "test",
    restore_dirs: list[Path] | None = None,
    exclude_files: list[Path] | None = None,
    includes: list[list[str]] | None = None,
    excludes: list[str | None] | None = None,
) -> RsyncMethod:
    """Build a backend configured the way `_build_backend` would."""
    return backend_cls({
        "destination": str(dest),
        "_name": name,
        "_source_paths": [str(p) for p in sources],
        "_exclude_files": [str(p) for p in exclude_files or []],
        "_source_includes": includes or [[] for _ in sources],
        "_source_excludes": excludes or [None for _ in sources],
        "_restore_test_dirs": [str(p) for p in restore_dirs or []],
    })


def make_rsync(dest: Path, sources: list[Path], **kwargs: object) -> RsyncMethod:
    return make_method(RsyncMethod, dest, sources, **kwargs)


def _status(dest: Path, name: str = "test") -> dict:
    return json.loads((dest / f"bu-{name}-status.txt").read_text())


def _source(env: Path, name: str = "src") -> Path:
    src = env / name
    (src / "sub").mkdir(parents=True)
    (src / "top.txt").write_text("top\n")
    (src / "sub" / "b.txt").write_text("b\n")
    return src


def test_marker_is_written_backed_up_and_verified(env: Path) -> None:
    src = _source(env)
    dest = env / "dest"
    dest.mkdir()
    m = make_rsync(dest, [src], restore_dirs=[src])

    result = m.backup([str(src)], scroll_lines=0)
    assert not result["errors"]

    # Step 3a/3b: the directory holds a marker whose newest entry is the run id.
    marker = src / "backup-status-test.txt"
    assert marker.is_file()
    run_id = last_entry(marker.read_text())
    assert run_id

    # Step 4: the marker is backed up as a normal file, and the status file
    # records everything `bu restore-test` needs.
    assert (dest / "src" / "backup-status-test.txt").is_file()
    status = _status(dest)
    assert status["run_id"] == run_id
    assert status["restore_test"] == [{
        "dir": str(src),
        "source": str(src.resolve()),
        "source_subdir": "src",
        "rel_dir": "",
        "marker_file": "backup-status-test.txt",
        "run_id": run_id,
    }]

    verified = m.restore_test()
    assert verified["ok"], verified["errors"]
    assert (verified["checked"], verified["passed"], verified["failed"]) == (1, 1, 0)
    assert verified["results"][0]["actual"] == run_id
    assert verified["results"][0]["backup_path"] == "src/backup-status-test.txt"
    assert verified["results"][0]["ok"] is True


def test_stale_marker_fails_and_reports_both_values(env: Path) -> None:
    src = _source(env)
    dest = env / "dest"
    dest.mkdir()
    m = make_rsync(dest, [src], restore_dirs=[src])
    m.backup([str(src)], scroll_lines=0)
    run_id = last_entry((src / "backup-status-test.txt").read_text())

    # Simulate a backup that captured an older run than the status claims.
    with open(dest / "src" / "backup-status-test.txt", "a") as fh:
        fh.write("stale-entry\n")

    verified = m.restore_test()
    assert verified["ok"] is False
    assert (verified["checked"], verified["passed"], verified["failed"]) == (1, 0, 1)
    outcome = verified["results"][0]
    assert outcome["actual"] == "stale-entry"
    assert outcome["expected"] == run_id
    assert "stale-entry" in outcome["error"] and run_id in outcome["error"]
    # The failure names the offending marker for the operator.
    assert any(str(src) in err for err in verified["errors"])


def test_missing_marker_in_backup_fails(env: Path) -> None:
    src = _source(env)
    dest = env / "dest"
    dest.mkdir()
    m = make_rsync(dest, [src], restore_dirs=[src])
    m.backup([str(src)], scroll_lines=0)
    (dest / "src" / "backup-status-test.txt").unlink()

    verified = m.restore_test()
    assert verified["ok"] is False
    assert verified["results"][0]["actual"] is None
    assert "backup-status-test.txt" in verified["results"][0]["error"]


def test_temporary_directory_is_always_removed(
    env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = _source(env)
    dest = env / "dest"
    dest.mkdir()
    m = make_rsync(dest, [src], restore_dirs=[src])
    m.backup([str(src)], scroll_lines=0)

    created: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def recording_mkdtemp(*args: object, **kwargs: object) -> str:
        path = real_mkdtemp(*args, **kwargs)
        created.append(path)
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", recording_mkdtemp)

    assert m.restore_test()["ok"]
    assert created and not Path(created[0]).exists()

    # Also on failure: no evidence left on disk, the findings are returned.
    (dest / "src" / "backup-status-test.txt").write_text("nope\n")
    created.clear()
    assert not m.restore_test()["ok"]
    assert created and not Path(created[0]).exists()


def test_restore_test_without_configured_dirs_is_not_a_failure(env: Path) -> None:
    src = _source(env)
    dest = env / "dest"
    dest.mkdir()
    m = make_rsync(dest, [src])

    verified = m.restore_test()
    assert verified["ok"] is True
    assert verified["checked"] == 0
    assert verified["errors"] == ["No restore-test directories configured."]
    assert "No restore-test markers were checked." in format_restore_test(verified)


def test_configured_but_never_backed_up_reports_clearly(env: Path) -> None:
    src = _source(env)
    dest = env / "dest"
    dest.mkdir()
    m = make_rsync(dest, [src], restore_dirs=[src])

    verified = m.restore_test()
    assert verified["ok"] is False
    assert "run 'bu backup test' first" in verified["errors"][0]


def test_action_refuses_while_the_backup_is_not_completed(env: Path) -> None:
    src = _source(env)
    dest = env / "dest"
    dest.mkdir()
    dest_cfg = DestinationConfig("test", {
        "method": "rsync",
        "source_paths": [str(src)],
        "destination": str(dest),
        "restore_test_dirs": [str(src)],
    })
    (dest / "bu-test-status.txt").write_text(json.dumps({"state": "started"}))

    result = action_restore_test(dest_cfg)
    assert result["ok"] is False
    assert "state 'started'" in result["errors"][0]
    assert "blocked until the backup completes" in result["errors"][1]


def test_snapshot_run_id_is_the_snapshot_directory(env: Path) -> None:
    src = _source(env)
    dest = env / "dest"
    dest.mkdir()
    m = make_method(SnapshotMethod, dest, [src], restore_dirs=[src])

    first = m.backup([str(src)], scroll_lines=0)
    assert not first["errors"]
    snap1 = first["snapshot"]
    status = _status(dest)
    assert status["run_id"] == snap1
    assert status["snapshot"] == snap1

    second = m.backup([str(src)], scroll_lines=0)
    snap2 = second["snapshot"]
    assert snap2 != snap1

    # One entry per run — the marker accumulates the history.
    marker = dest / snap2 / "src" / "backup-status-test.txt"
    assert marker.read_text().split() == [snap1, snap2]

    verified = m.restore_test()
    assert verified["ok"], verified["errors"]
    # Verified against the recorded snapshot, not merely the newest one.
    assert verified["expected"] == snap2
    assert verified["results"][0]["backup_path"] == f"{snap2}/src/backup-status-test.txt"


def test_restarted_snapshot_does_not_duplicate_the_marker(env: Path) -> None:
    src = _source(env)
    dest = env / "dest"
    dest.mkdir()
    m = make_method(SnapshotMethod, dest, [src], restore_dirs=[src])

    first = m.backup([str(src)], scroll_lines=0)
    snap = first["snapshot"]
    marker = src / "backup-status-test.txt"
    assert marker.read_text().split() == [snap]

    # bu backup-restart resumes the SAME timestamped directory, so the run id
    # is unchanged and must not be appended twice.
    again = m.backup([str(src)], scroll_lines=0, extra_args={"restart": True})
    assert again["snapshot"] == snap
    assert (dest / snap / "src" / "backup-status-test.txt").read_text().split() == [snap]


def test_duplicate_source_basenames_keep_distinct_marker_paths(env: Path) -> None:
    first = _source(env, "a/src")
    second = _source(env, "b/src")
    dest = env / "dest"
    dest.mkdir()
    m = make_method(SnapshotMethod, dest, [first, second], restore_dirs=[first, second])

    result = m.backup([str(first), str(second)], scroll_lines=0)
    assert not result["errors"]
    snap = result["snapshot"]

    # The second source's archive subdir is deduped, and the marker record
    # must name the same subdir or the restore would read the wrong archive.
    status = _status(dest)
    assert [e["source_subdir"] for e in status["restore_test"]] == ["src", "src_2"]
    assert (dest / snap / "src_2" / "backup-status-test.txt").is_file()

    verified = m.restore_test()
    assert verified["ok"], verified["errors"]
    assert [r["backup_path"] for r in verified["results"]] == [
        f"{snap}/src/backup-status-test.txt",
        f"{snap}/src_2/backup-status-test.txt",
    ]


def test_nested_sources_are_owned_by_the_longest_match(env: Path) -> None:
    outer = _source(env, "outer")
    inner = outer / "inner"
    (inner / "sub").mkdir(parents=True)
    (inner / "top.txt").write_text("inner\n")
    dest = env / "dest"
    dest.mkdir()
    m = make_method(
        SnapshotMethod, dest, [outer, inner], restore_dirs=[inner],
    )

    markers, warnings = m.resolve_restore_markers([str(outer), str(inner)])
    assert warnings == []
    assert markers[0]["source"] == str(inner.resolve())
    assert markers[0]["source_subdir"] == "inner"
    assert markers[0]["rel_dir"] == ""


def test_marker_outside_every_source_tree_warns(env: Path) -> None:
    src = _source(env)
    outside = env / "elsewhere"
    outside.mkdir()
    dest = env / "dest"
    dest.mkdir()
    m = make_rsync(dest, [src], restore_dirs=[outside])

    markers, warnings = m.resolve_restore_markers([str(src)])
    assert markers == []
    assert len(warnings) == 1
    assert "not inside any source path" in warnings[0]
    # Surfaced by both `bu status` (notes) and `bu backup` (preflight).
    assert m.restore_test_notes() == warnings
    assert m.preflight_notes() == warnings


def test_marker_at_the_source_root_needs_no_include_entry(env: Path) -> None:
    src = _source(env)
    dest = env / "dest"
    dest.mkdir()
    inc = env / "include.txt"
    inc.write_text("sub\n")
    m = make_rsync(dest, [src], restore_dirs=[src], includes=[[str(inc)]])

    # bu lets the marker filename through the include filter itself, so a
    # marker at the root must not warn...
    assert m.restore_test_notes() == []

    # ...and the real backup must actually contain it.
    result = m.backup([str(src)], scroll_lines=0)
    assert not result["errors"]
    assert (dest / "src" / "backup-status-test.txt").is_file()
    assert (dest / "src" / "sub" / "b.txt").is_file()
    assert not (dest / "src" / "top.txt").exists()
    assert m.restore_test()["ok"]


def test_marker_deeper_than_the_include_list_warns(env: Path) -> None:
    src = _source(env)
    dest = env / "dest"
    dest.mkdir()
    inc = env / "include.txt"
    inc.write_text("top.txt\n")  # does not cover sub/
    m = make_rsync(dest, [src], restore_dirs=[src / "sub"], includes=[[str(inc)]])

    notes = m.restore_test_notes()
    assert len(notes) == 1
    assert "include list" in notes[0]
    assert "sub/backup-status-test.txt" in notes[0]


def test_exclusion_pattern_matching_the_marker_warns(env: Path) -> None:
    src = _source(env)
    dest = env / "dest"
    dest.mkdir()
    exc = env / "exclude.txt"
    exc.write_text("*.txt\n")
    m = make_rsync(dest, [src], restore_dirs=[src], exclude_files=[exc])

    notes = m.restore_test_notes()
    assert any("exclusion pattern" in n for n in notes)


@pytest.mark.parametrize(("pattern", "rel_path", "expected"), [
    ("Documents", "Documents", True),
    ("Documents", "a/Documents/b", True),          # bare pattern: any depth
    ("Documents", "Documents2", False),
    ("/Documents", "Documents", True),
    ("/Documents", "a/Documents", False),          # anchored to the source root
    ("Library/Logs", "Library/Logs", True),
    ("Library/Logs", "x/Library/Logs", False),     # contains '/' -> anchored
    ("*.log", "sub/x.log", True),
    ("**/cache", "a/b/cache", True),
    ("# comment", "anything", False),
    ("+ keep.txt", "keep.txt", True),
])
def test_exclusion_pattern_matching(pattern: str, rel_path: str, expected: bool) -> None:
    assert pattern_matches(pattern, rel_path) is expected


def test_format_restore_test_reports_failures(env: Path) -> None:
    src = _source(env)
    dest = env / "dest"
    dest.mkdir()
    m = make_rsync(dest, [src], restore_dirs=[src])
    m.backup([str(src)], scroll_lines=0)
    (dest / "src" / "backup-status-test.txt").write_text("nope\n")

    output = format_restore_test(m.restore_test())
    assert "✗ 0 of 1 marker(s) verified" in output
    assert "NOT fully restorable" in output


def _case_insensitive_fs(directory: Path) -> bool:
    """True when the filesystem holding ``directory`` ignores filename case."""
    probe = directory / "bu-case-probe"
    probe.mkdir(exist_ok=True)
    try:
        return (directory / "BU-CASE-PROBE").is_dir()
    finally:
        probe.rmdir()


def test_on_disk_path_keeps_components_that_do_not_exist(env: Path) -> None:
    missing = env / "not-created-yet"
    assert on_disk_path(missing) == missing


def test_directory_spelling_is_recorded_as_it_is_on_disk(env: Path) -> None:
    """A case-variant entry must be recorded with the on-disk spelling.

    macOS volumes are case-insensitive but case-preserving, so a configured
    ``.../backups`` can name a directory actually called ``Backups``.  The
    backup copies the on-disk name, so recording the configured spelling builds
    a path that does not exist when the destination is a case-sensitive volume
    (Case-sensitive APFS) — which is exactly what `bu restore-test` then
    reported as "Backup path not found".
    """
    if not _case_insensitive_fs(env):
        pytest.skip("needs a case-insensitive filesystem")

    src = env / "src"
    (src / "Backups").mkdir(parents=True)
    (src / "Backups" / "old.gpg").write_text("x\n")
    dest = env / "dest"
    dest.mkdir()
    m = make_rsync(dest, [src], restore_dirs=[src / "backups"])

    markers, warnings = m.resolve_restore_markers([str(src)])
    assert warnings == []
    assert markers[0]["rel_dir"] == "Backups"
    assert markers[0]["dir"] == str(src / "Backups")

    result = m.backup([str(src)], scroll_lines=0)
    assert not result["errors"]
    status = _status(dest)
    assert status["restore_test"][0]["rel_dir"] == "Backups"
    assert m.restore_test()["results"][0]["backup_path"] == (
        "src/Backups/backup-status-test.txt"
    )


def test_wizard_lists_each_source_root(
    env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bu.wizard import run_create_wizard

    cfg_dir = env / "config"
    first = _source(env, "one")
    second = _source(env, "two")
    dest = env / "dest"
    dest.mkdir()

    # Answers: Directory, destination, snapshot, source one, yes, source two,
    # no more sources.
    monkeypatch.setattr(
        sys, "stdin",
        io.StringIO(f"1\n{dest}\n1\n{first}\ny\n{second}\nn\n"),
    )
    result = run_create_wizard(cfg_dir, name="docs")
    assert result["ok"]

    config = (cfg_dir / "docs.toml").read_text()
    assert "restore_test_dirs = [" in config
    assert f'    "{first}",' in config
    assert f'    "{second}",' in config

    cfg = Config(cfg_dir).get("docs")
    assert cfg.restore_test_dirs == [first, second]

    # Validation is clean: every entry sits inside a source tree and reaches
    # the backup, so `bu status`/`bu backup` have nothing to warn about.
    assert _build_backend(cfg).restore_test_notes() == []


# ---------------------------------------------------------------------------
# duplicity: the passphrase a restore test needs
# ---------------------------------------------------------------------------

RUN_ID = "2026-09-28T19:47:13.059023+00:00"
MARKER = "backup-status-b2.txt"


class Tty(io.StringIO):
    """A stdin stand-in that claims to be a terminal."""

    def isatty(self) -> bool:
        return True


def _duplicity_stub(env: Path, run_id: str) -> Path:
    """Write a fake duplicity that fabricates the restored marker file.

    ``duplicity restore --path-to-restore=<file> <target>`` writes the file
    *at* ``target``, replacing the placeholder directory bu creates there, so
    the stub does the same.  (Verified against duplicity 3.1.0.)
    """
    directory = env / "bin"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "duplicity"
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "duplicity 3.1.0"; exit 0; fi\n'
        # The target is the last argument.
        'target=""\n'
        'for a in "$@"; do target="$a"; done\n'
        '[ -d "$target" ] && rmdir "$target"\n'
        f'printf "%s\\n" "{run_id}" > "$target"\n'
    )
    os.chmod(path, 0o755)
    return path


def make_duplicity(
    env: Path,
    sources: list[Path],
    *,
    name: str = "b2",
    passphrase_file: Path | None = None,
) -> DuplicityMethod:
    """A duplicity backend with one restore marker per source."""
    config: dict = {
        "destination": str(env / "dest"),
        "_name": name,
        "_source_paths": [str(s) for s in sources],
        "_exclude_files": [],
        "_source_includes": [[] for _ in sources],
        "_source_excludes": [None for _ in sources],
        "_restore_test_dirs": [str(s) for s in sources],
    }
    if passphrase_file is not None:
        config["passphrase_file"] = str(passphrase_file)
    return DuplicityMethod(config)


def record_run(
    method: DuplicityMethod, sources: list[Path], run_id: str = RUN_ID,
) -> None:
    """Record a completed run the way `bu backup` would."""
    method._write_status(
        "completed",
        run_id=run_id,
        restore_test=[
            {
                "dir": str(s),
                "source": str(s.resolve()),
                "source_subdir": s.name,
                "rel_dir": "",
                "marker_file": MARKER,
                "run_id": run_id,
            }
            for s in sources
        ],
    )


def test_duplicity_restore_test_prompts_once_on_a_terminal(
    env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interactive restore-test asks for the passphrase instead of failing.

    Regression: the passphrase was resolved non-interactively even on a
    terminal, so `bu restore-test` failed unless the passphrase happened to be
    in `passphrase_file` or the environment — even though `bu backup` prompts.
    """
    sources = []
    for label in ("paul", "Dropbox", "GoogleDrive"):
        source = env / label
        source.mkdir()
        sources.append(source)
    _duplicity_stub(env, RUN_ID)
    monkeypatch.setenv("PATH", f"{env / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("BU_PASSPHRASE", raising=False)
    monkeypatch.delenv("PASSPHRASE", raising=False)
    monkeypatch.setattr(sys, "stdin", Tty())

    prompts: list[str] = []

    def fake_getpass(prompt: str) -> str:
        prompts.append(prompt)
        return "hunter2"

    monkeypatch.setattr(duplicity, "getpass", SimpleNamespace(getpass=fake_getpass))

    method = make_duplicity(env, sources)
    record_run(method, sources)

    result = method.restore_test()
    assert result["ok"], result["errors"]
    assert (result["checked"], result["passed"], result["failed"]) == (3, 3, 0)
    # One prompt for three markers: the passphrase is cached for the run.
    assert prompts == ["GPG passphrase for 'b2': "]


def test_duplicity_restore_test_never_prompts_without_a_terminal(
    env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unattended restore test fails with a hint rather than blocking."""
    source = env / "src"
    source.mkdir()
    _duplicity_stub(env, RUN_ID)
    monkeypatch.setenv("PATH", f"{env / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("BU_PASSPHRASE", raising=False)
    monkeypatch.delenv("PASSPHRASE", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO())

    prompts: list[str] = []
    monkeypatch.setattr(
        duplicity, "getpass",
        SimpleNamespace(getpass=lambda prompt: prompts.append(prompt) or "x"),
    )

    method = make_duplicity(env, [source])
    record_run(method, [source])

    result = method.restore_test()
    assert result["ok"] is False
    assert "No passphrase available for 'b2'" in result["errors"][0]
    assert "run from a terminal to be prompted" in result["errors"][0]
    assert prompts == []


def test_duplicity_restore_test_uses_a_passphrase_file(
    env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`passphrase_file` keeps the restore test unattended and prompt-free."""
    source = env / "src"
    source.mkdir()
    _duplicity_stub(env, RUN_ID)
    monkeypatch.setenv("PATH", f"{env / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("BU_PASSPHRASE", raising=False)
    monkeypatch.delenv("PASSPHRASE", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO())

    passphrase_file = env / "b2.pass"
    passphrase_file.write_text("hunter2\n")

    prompts: list[str] = []
    monkeypatch.setattr(
        duplicity, "getpass",
        SimpleNamespace(getpass=lambda prompt: prompts.append(prompt) or "x"),
    )

    method = make_duplicity(env, [source], passphrase_file=passphrase_file)
    record_run(method, [source])

    result = method.restore_test()
    assert result["ok"], result["errors"]
    assert prompts == []
