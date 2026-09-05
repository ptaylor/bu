"""Tests for per-source include/exclude filter files."""

from __future__ import annotations

from pathlib import Path

import pytest

from bu.actions import _build_backend, format_status
from bu.backends.duplicity import DuplicityMethod
from bu.backends.local import RsyncMethod
from bu.backends.snapshot import SnapshotMethod
from bu.config import Config, ConfigError
from bu.filters import build_include_rules


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point bu's config/log/state dirs at tmp_path (AGENTS.md convention)."""
    monkeypatch.setenv("BU_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("BU_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path


def make_rsync(
    dest: Path, src: Path, include: list[str] | None, exclude: list[str] | None,
) -> RsyncMethod:
    config: dict = {
        "destination": str(dest),
        "_name": "test",
        "_source_paths": [str(src)],
        "_exclude_files": [],
        "_source_includes": [include or []],
        "_source_excludes": [exclude],
    }
    return RsyncMethod(config)


def test_build_include_rules() -> None:
    assert build_include_rules(["docs"]) == ["+ /docs", "+ /docs/**"]
    assert build_include_rules(["a/b/c.txt"]) == [
        "+ /a/", "+ /a/b/", "+ /a/b/c.txt", "+ /a/b/c.txt/**",
    ]
    assert build_include_rules(["."]) == []


def test_config_parses_per_source_entries(env: Path) -> None:
    cfg_dir = env / "config"
    cfg_dir.mkdir()
    (cfg_dir / "docs.toml").write_text(
        'method = "rsync"\n'
        "source_paths = [\n"
        '    { path = "/Users/paul", include = "~/.config/bu/inc.txt",'
        ' exclude = ["~/.config/bu/ex1.txt", "~/.config/bu/ex2.txt"] },\n'
        '    "/Volumes/data",\n'
        "]\n"
        'destination = "/mnt/backup"\n'
    )
    d = Config(cfg_dir).get("docs")
    assert d.source_paths == ["/Users/paul", "/Volumes/data"]
    assert d.per_source_include_files == [
        [Path("~/.config/bu/inc.txt").expanduser()],
        [],
    ]
    assert d.per_source_exclude_files == [
        [Path(p).expanduser() for p in ("~/.config/bu/ex1.txt", "~/.config/bu/ex2.txt")],
        None,
    ]


@pytest.mark.parametrize(
    "sources, fragment",
    [
        ('source_paths = [{ path = 3 }]', "path"),
        ('source_paths = [{ path = "/a", bogus = 1 }]', "bogus"),
        ('source_paths = [{ path = "/a", include = 3 }]', "include"),
        ("source_paths = [3]", "tables"),
    ],
)
def test_config_rejects_bad_source_entries(
    env: Path, sources: str, fragment: str,
) -> None:
    cfg_dir = env / "config"
    cfg_dir.mkdir()
    (cfg_dir / "bad.toml").write_text(
        f'method = "rsync"\n{sources}\ndestination = "/mnt"\n'
    )
    with pytest.raises(ConfigError) as exc:
        Config(cfg_dir)
    assert fragment in str(exc.value)


def test_build_backend_passes_per_source_lists(env: Path) -> None:
    cfg_dir = env / "config"
    cfg_dir.mkdir()
    (cfg_dir / "d.toml").write_text(
        'method = "rsync"\n'
        'source_paths = [{ path = "/a", include = "/i.txt", exclude = "/e.txt" }]\n'
        'destination = "/d"\n'
    )
    backend = _build_backend(Config(cfg_dir).get("d"))
    assert backend.config["_source_includes"] == [["/i.txt"]]
    assert backend.config["_source_excludes"] == [["/e.txt"]]


def test_rsync_include_list_backs_up_only_listed(env: Path) -> None:
    src = env / "src"
    (src / "docs").mkdir(parents=True)
    (src / "other").mkdir(parents=True)
    (src / "docs" / "a.txt").write_text("a\n")
    (src / "other" / "b.txt").write_text("b\n")
    (src / "notes.txt").write_text("n\n")
    dest = env / "dest"
    dest.mkdir()
    inc = env / "include.txt"
    inc.write_text("docs\nnotes.txt\n")

    m = make_rsync(dest, src, include=[str(inc)], exclude=None)
    r = m.backup([str(src)], scroll_lines=0)
    assert not r["errors"]
    assert (dest / "src" / "docs" / "a.txt").exists()
    assert (dest / "src" / "notes.txt").exists()
    assert not (dest / "src" / "other").exists()


def test_rsync_include_with_exclude_file(env: Path) -> None:
    src = env / "src"
    (src / "docs").mkdir(parents=True)
    (src / "docs" / "keep.txt").write_text("k\n")
    (src / "docs" / "skip.txt").write_text("s\n")
    dest = env / "dest"
    dest.mkdir()
    inc = env / "include.txt"
    inc.write_text("docs\n")
    exc = env / "exclude.txt"
    exc.write_text("skip.txt\n")

    m = make_rsync(dest, src, include=[str(inc)], exclude=[str(exc)])
    r = m.backup([str(src)], scroll_lines=0)
    assert not r["errors"]
    assert (dest / "src" / "docs" / "keep.txt").exists()
    assert not (dest / "src" / "docs" / "skip.txt").exists()


def test_rsync_include_removed_lines_stay_in_backup(env: Path) -> None:
    src = env / "src"
    (src / "docs").mkdir(parents=True)
    (src / "docs" / "a.txt").write_text("a\n")
    (src / "notes.txt").write_text("n\n")
    dest = env / "dest"
    dest.mkdir()
    inc = env / "include.txt"
    inc.write_text("docs\nnotes.txt\n")

    m = make_rsync(dest, src, include=[str(inc)], exclude=None)
    assert not m.backup([str(src)], scroll_lines=0)["errors"]
    assert (dest / "src" / "notes.txt").exists()

    # Removing a line stops updates but never deletes (excluded → protected).
    inc.write_text("docs\n")
    assert not m.backup([str(src)], scroll_lines=0)["errors"]
    assert (dest / "src" / "notes.txt").exists()
    assert (dest / "src" / "docs" / "a.txt").exists()


def test_snapshot_include_list(env: Path) -> None:
    src = env / "src"
    (src / "docs").mkdir(parents=True)
    (src / "other").mkdir(parents=True)
    (src / "docs" / "a.txt").write_text("a\n")
    (src / "other" / "b.txt").write_text("b\n")
    dest = env / "dest"
    dest.mkdir()
    inc = env / "include.txt"
    inc.write_text("docs\n")

    m = SnapshotMethod({
        "destination": str(dest),
        "_name": "test",
        "_source_paths": [str(src)],
        "_exclude_files": [],
        "_source_includes": [[str(inc)]],
        "_source_excludes": [None],
    })
    r1 = m.backup([str(src)], scroll_lines=0)
    assert not r1["errors"]
    snap1 = dest / r1["snapshot"]
    assert (snap1 / "src" / "docs" / "a.txt").exists()
    assert not (snap1 / "src" / "other").exists()

    r2 = m.backup([str(src)], scroll_lines=0)
    assert not r2["errors"]
    snap2 = dest / r2["snapshot"]
    # Unchanged included file is hard-linked to the previous snapshot.
    f1 = snap1 / "src" / "docs" / "a.txt"
    f2 = snap2 / "src" / "docs" / "a.txt"
    assert f1.stat().st_ino == f2.stat().st_ino

    st = m.status()
    assert st["sources"][0]["include_files"] == [str(inc)]


def test_wizard_creates_include_file_and_commented_example(
    env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io
    import sys

    from bu.wizard import run_create_wizard

    cfg_dir = env / "config"
    src = env / "src"
    src.mkdir()
    dest = env / "dest"
    dest.mkdir()

    # Answers: backup type Directory (1), destination, method Rsync (2),
    # source path, no more sources (n).
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"1\n{dest}\n2\n{src}\nn\n"))
    result = run_create_wizard(cfg_dir, name="docs")

    assert result["ok"]
    assert result["destination"]["method"] == "rsync"
    include_file = cfg_dir / "include-docs.txt"
    assert include_file.is_file()
    assert "Include list" in include_file.read_text()
    config = (cfg_dir / "docs.toml").read_text()
    assert "include-docs.txt" in config
    assert "#   source_paths = [{ path =" in config


def test_status_reports_filter_files(env: Path) -> None:
    src = env / "src"
    src.mkdir()
    dest = env / "dest"
    dest.mkdir()
    inc = env / "inc.txt"
    inc.write_text("docs\n")
    exc = env / "exc.txt"
    exc.write_text("*.tmp\n")

    m = make_rsync(dest, src, include=[str(inc)], exclude=[str(exc)])
    st = m.status()
    assert st["sources"][0]["include_files"] == [str(inc)]
    assert st["sources"][0]["exclude_files"] == [str(exc)]

    # No per-source exclude → destination-level defaults are reported.
    m2 = make_rsync(dest, src, include=None, exclude=None)
    m2.config["_exclude_files"] = [str(exc)]
    st2 = m2.status()
    assert st2["sources"][0]["include_files"] == []
    assert st2["sources"][0]["exclude_files"] == [str(exc)]


def test_format_status_shows_filter_files() -> None:
    out = format_status({
        "name": "docs",
        "method": "rsync",
        "sources": [
            {
                "path": "/Users/paul",
                "exists": True,
                "include_files": ["/Users/paul/.config/bu/include-docs.txt"],
                "exclude_files": [
                    "/Users/paul/.config/bu/exclude-docs.txt",
                    "/Users/paul/.config/bu/missing.txt",
                ],
            },
        ],
        "dest_path": "/mnt/backup",
        "dest_exists": True,
        "last_backup": None,
    })
    assert "include: /Users/paul/.config/bu/include-docs.txt" in out
    assert "exclude: /Users/paul/.config/bu/exclude-docs.txt" in out
    assert "missing.txt (missing)" in out


def test_format_status_remote_dest_exists_is_na() -> None:
    out = format_status({
        "name": "docs",
        "method": "duplicity",
        "sources": [],
        "dest_path": "b2://bucket/docs",
        "dest_exists": None,
        "last_backup": None,
    })
    assert "Dest exists  : n/a (remote destination)" in out


def test_format_status_shows_common_exclude_files() -> None:
    out = format_status({
        "name": "docs",
        "method": "rsync",
        "sources": [],
        "dest_path": "/mnt/backup",
        "dest_exists": True,
        "exclude_files": [
            "/Users/paul/.config/bu/exclude.txt",
            "/Users/paul/.config/bu/missing.txt",
        ],
        "last_backup": None,
    })
    assert "Exclude files:" in out
    assert "/Users/paul/.config/bu/exclude.txt" in out
    assert "missing.txt (missing)" in out


def test_duplicity_include_and_exclude_args(env: Path) -> None:
    inc = env / "include.txt"
    inc.write_text("docs\ncode/proj\n")
    exc = env / "exclude.txt"
    exc.write_text("*.tmp\n")

    m = DuplicityMethod({
        "destination": "/tmp/archives",
        "_name": "test",
        "_source_paths": ["/Users/paul"],
        "_exclude_files": [str(exc)],
        "_source_includes": [[str(inc)]],
        "_source_excludes": [None],
    })

    prefix, close = m._include_args(Path("/Users/paul"), 0)
    assert prefix == [
        "--include=/Users/paul/docs",
        "--include=/Users/paul/code/proj",
    ]
    assert close == ["--exclude=**"]

    # No include files → no include args, destination defaults still used.
    plain = DuplicityMethod({
        "destination": "/tmp/archives",
        "_name": "test",
        "_source_paths": ["/Users/paul"],
        "_exclude_files": [str(exc)],
        "_source_includes": [],
        "_source_excludes": [None],
    })
    assert plain._include_args(Path("/Users/paul"), 0) == ([], [])
    assert plain._source_exclude_files(0) == [str(exc)]
    args = plain._exclude_args(Path("/Users/paul"), plain._source_exclude_files(0))
    assert "--exclude=**/*.tmp" in args
