"""Tests for the sample config templates used by 'bu config NAME'."""

import tomllib

from bu.actions import _sample_for_method


def _render(method, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("BU_LOG_DIR", str(tmp_path / "logs"))
    return _sample_for_method(method, "demo")


def test_method_samples_render_and_parse(tmp_path, monkeypatch):
    for method in ("rsync", "duplicity", "snapshot"):
        content = _render(method, tmp_path, monkeypatch)
        assert "history_file =" in content
        assert "log_file =" in content
        assert tomllib.loads(content)["method"] == method


def test_generic_sample_renders(tmp_path, monkeypatch):
    # The generic template's commented inline-table example contains literal
    # { } braces, which used to crash template.format() with a KeyError.
    content = _render("bogus-method", tmp_path, monkeypatch)
    assert '{ path = "/path/to/backup", include = ' in content
    assert "history_file =" in content
    parsed = tomllib.loads(content)
    assert set(parsed) == {"history_file", "log_file"}
