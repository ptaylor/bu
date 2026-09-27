"""Tests for `bu backup-dry-run` — listing the files a backup would consider."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bu.actions import action_backup_dry_run
from bu.backends import local
from bu.backends.duplicity import DuplicityMethod, _looks_like_error
from bu.backends.local import RsyncMethod
from bu.config import Config


def _stub_binary(directory: Path, name: str, body: str) -> Path:
    """Write an executable shell stub named ``name`` into ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\n" + body)
    os.chmod(path, 0o755)
    return path


def _write_config(config_dir: Path, name: str, body: str) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / f"{name}.toml").write_text(body)


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point bu's config/log/state dirs at tmp_path (AGENTS.md convention)."""
    monkeypatch.setenv("BU_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("BU_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path


def _rsync_config(env: Path, own_exclude: Path) -> str:
    """A rsync destination whose source also has its own exclude file."""
    return (
        'method = "rsync"\n'
        "source_paths = [\n"
        f'    {{ path = "{env / "src"}", exclude = "{own_exclude}" }},\n'
        "]\n"
        f'destination = "{env / "dest"}"\n'
    )


def test_rsync_listing_uses_every_exclude_file_and_writes_nothing(
    env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (env / "src").mkdir()
    (env / "dest").mkdir()
    argv_file = env / "argv.txt"
    stub = _stub_binary(
        env / "bin", "rsync",
        f'printf "%s\\n" "$@" > {argv_file}\n'
        "echo ./\n"
        "echo a.txt\n"
        "echo sub/\n"
        "echo sub/c.txt\n",
    )
    monkeypatch.setenv("BU_RSYNC", str(stub))
    monkeypatch.setattr(local, "_RSYNC_CACHE", None)

    global_ex = env / "global.txt"
    global_ex.write_text("skip.log\n")
    own_ex = env / "own.txt"
    own_ex.write_text("b.txt\n")

    backend = RsyncMethod({
        "destination": str(env / "dest"),
        "_name": "t",
        "_source_paths": [str(env / "src")],
        "_exclude_files": [str(global_ex)],
        "_source_includes": [[]],
        "_source_excludes": [[str(own_ex)]],
    })
    result = backend.list_files([str(env / "src")])

    assert result["errors"] == []
    assert result["total"] == 4
    assert result["sources"][0]["paths"] == [".", "a.txt", "sub/", "sub/c.txt"]

    argv = argv_file.read_text().splitlines()
    # Additive: the source's own file first, then the destination defaults.
    assert f"--exclude-from={own_ex}" in argv
    assert f"--exclude-from={global_ex}" in argv
    assert argv.index(f"--exclude-from={own_ex}") < argv.index(f"--exclude-from={global_ex}")
    assert "-n" in argv
    # The listing target must not exist, so nothing is compared (or written).
    assert not Path(argv[-1]).exists()
    assert list((env / "dest").iterdir()) == []


def test_duplicity_listing_is_relative_and_leaves_no_trace(
    env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = env / "src"
    (src / "sub").mkdir(parents=True)
    argv_file = env / "argv.txt"
    stub_dir = env / "bin"
    _stub_binary(
        stub_dir, "duplicity",
        f'printf "%s\\n" "$@" > {argv_file}\n'
        f"echo 'Selecting {src}'\n"
        f"echo 'Selecting {src}/a.txt'\n"
        f"echo 'Selecting {src}/sub'\n"
        f"echo 'Selecting {src}/sub/c.txt'\n",
    )
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("PASSPHRASE", raising=False)
    monkeypatch.delenv("BU_PASSPHRASE", raising=False)

    backend = DuplicityMethod({
        "destination": str(env / "dest"),
        "_name": "t",
        "_source_paths": [str(src)],
        "_exclude_files": [],
        "_source_includes": [[]],
        "_source_excludes": [None],
    })
    result = backend.list_files([str(src)])

    assert result["errors"] == []
    assert result["sources"][0]["paths"] == [".", "a.txt", "sub", "sub/c.txt"]

    argv = argv_file.read_text().splitlines()
    assert "--dry-run" in argv
    assert "--verbosity=9" in argv
    # A scratch target, so the real destination is never touched: no
    # credentials are sent and no archive is created.
    target = argv[-1]
    assert target.startswith("file://")
    assert not Path(target[len("file://"):]).exists()
    assert not (env / "dest").exists()
    archive_dir = next(a.split("=", 1)[1] for a in argv if a.startswith("--archive-dir="))
    assert not Path(archive_dir).exists()


def test_duplicity_listing_surfaces_the_real_error(
    env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed dry run reports duplicity's diagnostic, not its housekeeping."""
    src = env / "src"
    src.mkdir()
    stub_dir = env / "bin"
    _stub_binary(
        stub_dir, "duplicity",
        "echo 'Using temporary directory /var/folders/xx/duplicity-abc-tempdir'\n"
        "echo 'CommandLineError: Target should be url, not directory.'\n"
        "exit 23\n",
    )
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")

    backend = DuplicityMethod({
        "destination": str(env / "dest"),
        "_name": "t",
        "_source_paths": [str(src)],
        "_exclude_files": [],
        "_source_includes": [[]],
        "_source_excludes": [None],
    })
    result = backend.list_files([str(src)])

    assert result["total"] == 0
    assert result["errors"] == [f"{src}: CommandLineError: Target should be url, not directory."]


def test_listing_error_predicate() -> None:
    """Housekeeping lines must not be mistaken for the failure reason."""
    assert _looks_like_error("CommandLineError: Target should be url, not directory.")
    assert _looks_like_error("gpg: decryption failed: No secret key")
    assert not _looks_like_error("Using temporary directory /var/folders/xx/duplicity-abc")
    assert not _looks_like_error("Selecting /Users/paul/a.txt")


def test_listing_streams_progress_and_paths(
    env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sources and paths are reported as they are found, not at the end."""
    src = env / "src"
    (src / "sub").mkdir(parents=True)
    stub_dir = env / "bin"
    _stub_binary(
        stub_dir, "duplicity",
        f"echo 'Selecting {src}'\n"
        f"echo 'Selecting {src}/a.txt'\n"
        f"echo 'Selecting {src}/sub'\n"
        f"echo 'Selecting {src}/sub/c.txt'\n",
    )
    monkeypatch.setenv("PATH", f"{stub_dir}{os.pathsep}{os.environ['PATH']}")

    backend = DuplicityMethod({
        "destination": str(env / "dest"),
        "_name": "t",
        "_source_paths": [str(src)],
        "_exclude_files": [],
        "_source_includes": [[]],
        "_source_excludes": [None],
    })
    events: list[str] = []
    backend.list_files(
        [str(src)],
        on_source=lambda entry: events.append(f"source:{entry['source']}"),
        on_path=lambda path: events.append(f"path:{path}"),
    )

    assert events == [
        f"source:{src}",
        "path:.",
        "path:a.txt",
        "path:sub",
        "path:sub/c.txt",
    ]


def test_one_unreadable_source_does_not_hide_the_others(
    env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source rsync cannot read must not silently zero the later ones.

    Reported as ``N path(s)`` for every source, an unreadable source used to
    look like "nothing to back up" — making a real backup look pointless.
    """
    bad, good = env / "bad", env / "good"
    bad.mkdir()
    (good / "sub").mkdir(parents=True)
    stub = _stub_binary(
        env / "bin", "rsync",
        'case "$*" in\n'
        '  *bad/*) echo "rsync: opendir \\"${1}\\" failed: Operation not permitted" >&2; exit 23 ;;\n'
        "esac\n"
        "echo ./\n"
        "echo a.txt\n",
    )
    monkeypatch.setenv("BU_RSYNC", str(stub))
    monkeypatch.setattr(local, "_RSYNC_CACHE", None)

    backend = RsyncMethod({
        "destination": str(env / "dest"),
        "_name": "t",
        "_source_paths": [str(bad), str(good)],
        "_exclude_files": [],
        "_source_includes": [[], []],
        "_source_excludes": [[], []],
    })
    result = backend.list_files([str(bad), str(good)])

    assert result["errors"], "the failing source must still be reported"
    assert result["sources"][0]["errors"]
    assert result["sources"][0]["paths"] == []
    # The readable source is scanned regardless of the earlier failure.
    assert result["sources"][1]["paths"] == [".", "a.txt"]
    assert result["total"] == 2


def test_action_keeps_stdout_pipeable(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    (env / "src").mkdir()
    (env / "dest").mkdir()
    stub = _stub_binary(env / "bin", "rsync", "echo a.txt\necho b.txt\n")
    monkeypatch.setenv("BU_RSYNC", str(stub))
    monkeypatch.setattr(local, "_RSYNC_CACHE", None)

    own_ex = env / "own.txt"
    own_ex.write_text("b.txt\n")
    _write_config(env / "config", "t", _rsync_config(env, own_ex))
    # The destination-level default (exclude.txt in the config dir) must still
    # apply to a source that declares its own exclude file.
    global_ex = env / "config" / "exclude.txt"
    global_ex.write_text("skip.log\n")

    dest_cfg = Config(env / "config").get("t")
    result = action_backup_dry_run(dest_cfg)

    captured = capsys.readouterr()
    # The list is the result: pipe-clean stdout, context on stderr.
    assert captured.out == "a.txt\nb.txt\n"
    assert f"  exclude: {own_ex}\n" in captured.err
    assert f"  exclude: {global_ex}\n" in captured.err
    assert captured.err.index(str(own_ex)) < captured.err.index(str(global_ex))
    assert "scanning…" in captured.err
    assert "2 path(s) in 1 source(s)" in captured.err
    assert result["total"] == 2


def test_backend_default_reports_unsupported(tmp_path: Path) -> None:
    """A method without listing support fails gracefully rather than crashing."""
    from bu.backends.base import Backend

    class Bare(Backend):
        METHOD_NAME = "bare"

        def backup(self, *args, **kwargs):  # pragma: no cover - unused
            raise NotImplementedError

        def restore(self, *args, **kwargs):  # pragma: no cover - unused
            raise NotImplementedError

        def status(self, **kwargs):  # pragma: no cover - unused
            raise NotImplementedError

    result = Bare({}).list_files([])
    assert result["total"] == 0
    assert "not supported" in result["errors"][0]
    assert "bare" in result["errors"][0]
