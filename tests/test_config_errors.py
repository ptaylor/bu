"""Tests for tolerating invalid config files in 'bu list' and friends."""

import pytest
from click.testing import CliRunner

from bu.cli import main
from bu.config import Config, ConfigError


def _write(cfg_dir, name, text):
    (cfg_dir / f"{name}.toml").write_text(text)


@pytest.fixture()
def cfg_dir(tmp_path):
    return tmp_path


def test_strict_config_raises_on_broken_file(cfg_dir):
    _write(cfg_dir, "bad", "# just a comment\n")
    with pytest.raises(ConfigError) as exc:
        Config(cfg_dir)
    assert "bad.toml" in str(exc.value)
    assert "method" in str(exc.value)


def test_lenient_config_collects_errors(cfg_dir):
    _write(cfg_dir, "bad", "# just a comment\n")
    _write(cfg_dir, "good", 'method = "rsync"\nsource_paths = ["/a"]\ndestination = "/b"\n')
    cfg = Config(cfg_dir, strict=False)
    assert cfg.list_destinations() == ["good"]
    assert "bad" in cfg.errors
    assert "missing required 'method'" in cfg.errors["bad"]
    with pytest.raises(ConfigError) as exc:
        cfg.get("bad")
    assert "missing required 'method'" in str(exc.value)


def test_list_shows_invalid_configs(cfg_dir, monkeypatch):
    monkeypatch.setenv("BU_CONFIG_DIR", str(cfg_dir))
    _write(cfg_dir, "bad", "# just a comment\n")
    _write(cfg_dir, "good", 'method = "rsync"\nsource_paths = ["/a"]\ndestination = "/b"\n')
    result = CliRunner().invoke(main, ["list"])
    assert result.exit_code == 1
    assert "good" in result.output
    assert "bad" in result.output
    assert "invalid config" in result.output


def test_list_clean_configs_exits_zero(cfg_dir, monkeypatch):
    monkeypatch.setenv("BU_CONFIG_DIR", str(cfg_dir))
    _write(cfg_dir, "good", 'method = "rsync"\nsource_paths = ["/a"]\ndestination = "/b"\n')
    result = CliRunner().invoke(main, ["list"])
    assert result.exit_code == 0
    assert "good" in result.output
    assert "invalid" not in result.output


def test_rsync_rejects_duplicate_source_basenames(cfg_dir):
    _write(
        cfg_dir,
        "dup",
        'method = "rsync"\nsource_paths = ["/a/foo", "/b/foo"]\ndestination = "/mnt"\n',
    )
    cfg = Config(cfg_dir, strict=False)
    assert "dup" in cfg.errors
    assert "overwrite each other" in cfg.errors["dup"]
    with pytest.raises(ConfigError):
        Config(cfg_dir)


def test_other_methods_allow_duplicate_basenames(cfg_dir):
    _write(
        cfg_dir,
        "snap",
        'method = "snapshot"\nsource_paths = ["/a/foo", "/b/foo"]\ndestination = "/mnt"\n',
    )
    _write(
        cfg_dir,
        "dupc",
        'method = "duplicity"\nsource_paths = ["/a/foo", "/b/foo"]\ndestination = "/mnt"\n',
    )
    cfg = Config(cfg_dir, strict=False)
    assert not cfg.errors
    assert cfg.list_destinations() == ["dupc", "snap"]
