"""Configuration loading and management for bu.

Each destination is stored as a separate .toml file under the config directory.
The default config directory is ~/.config/bu/ (or $BU_CONFIG_DIR if set).

Example layout:

    ~/.config/bu/
    ├── photos.toml
    ├── docs.toml
    └── server.toml

Example file (photos.toml):

    method = "rsync"
    source_paths = ["/home/user/photos"]
    destination = "/mnt/backup/photos"

Each destination gets two tracking files under the XDG state directory
(~/.local/state/bu/):

    history/<name>.json   — structured JSON-lines action history
    logs/<name>.log       — raw execution output per run

Override with the optional ``history_file`` and ``log_file`` keys, or set
``BU_LOG_DIR`` to change the base log directory.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


class ConfigError(Exception):
    """Raised when configuration is invalid or missing."""


def _normalise_file_list(value: Any, key: str) -> list[str] | None:
    """Accept a string or a list of strings for per-source include/exclude."""
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return value
    raise ConfigError(f"{key!r} must be a string or a list of strings")


# Keys that are handled specially and NOT passed through to backend ``extra``.
_RESERVED_KEYS = frozenset({"method", "source_paths", "destination", "history_file", "log_file", "exclude_files"})

# Valid method names.
_VALID_METHODS = frozenset({"rsync", "duplicity", "snapshot"})

# Destination names may contain only letters, digits, underscore, dash.
VALID_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _default_state_dir() -> Path:
    """Return ``$XDG_STATE_HOME/bu`` (default ~/.local/state/bu)."""
    xdg_state = os.environ.get(
        "XDG_STATE_HOME",
        os.path.expanduser("~/.local/state"),
    )
    return Path(xdg_state) / "bu"


def _default_history_dir() -> Path:
    """Return the default directory for structured history files."""
    return _default_state_dir() / "history"


def _default_raw_log_dir() -> Path:
    """Return the default directory for raw execution log files.

    Uses $BU_LOG_DIR if set, otherwise ``<state>/logs``.
    """
    if env_dir := os.environ.get("BU_LOG_DIR"):
        return Path(env_dir)
    return _default_state_dir() / "logs"


class DestinationConfig:
    """Holds the parsed configuration for a single backup destination."""

    def __init__(self, name: str, data: dict[str, Any], config_dir: Path | None = None) -> None:
        self.name = name
        self.method: str = data.get("method", "")
        self.destination: str = data.get("destination", "")
        self._config_dir = config_dir
        self._history_file_override: str | None = data.get("history_file")
        self._log_file_override: str | None = data.get("log_file")
        self._exclude_files_override: list[str] | None = data.get("exclude_files")

        # source_paths entries are plain strings or tables with optional
        # per-source ``include`` / ``exclude`` filter files.
        self.source_paths: list[str] = []
        self._per_source_includes: list[list[str]] = []
        self._per_source_excludes: list[list[str] | None] = []
        for entry in data.get("source_paths", []):
            if isinstance(entry, str):
                src, include, exclude = entry, None, None
            elif isinstance(entry, dict):
                src = entry.get("path", "")
                if not isinstance(src, str) or not src:
                    raise ConfigError("source_paths table entries need a 'path' string")
                unknown = set(entry) - {"path", "include", "exclude"}
                if unknown:
                    raise ConfigError(
                        f"unknown source_paths entry key(s): {', '.join(sorted(unknown))} "
                        "(allowed: path, include, exclude)"
                    )
                include = _normalise_file_list(entry.get("include"), "include")
                exclude = _normalise_file_list(entry.get("exclude"), "exclude")
            else:
                raise ConfigError(
                    "source_paths entries must be paths or tables with a 'path' key"
                )
            self.source_paths.append(src)
            self._per_source_includes.append(include or [])
            self._per_source_excludes.append(exclude)

        self.extra: dict[str, Any] = {
            k: v for k, v in data.items()
            if k not in _RESERVED_KEYS
        }

    @property
    def history_file(self) -> Path:
        """Resolved path for structured JSON-lines action history.

        Default: ``~/.local/state/bu/history/<name>.json``.
        """
        if self._history_file_override:
            return Path(self._history_file_override).expanduser()
        return _default_history_dir() / f"{self.name}.json"

    @property
    def log_file(self) -> Path:
        """Resolved path for raw execution logs.

        Default: ``~/.local/state/bu/logs/<name>.log``.
        """
        if self._log_file_override:
            return Path(self._log_file_override).expanduser()
        return _default_raw_log_dir() / f"{self.name}.log"

    @property
    def exclude_files(self) -> list[Path]:
        """Exclusion files for backups.

        Defaults to a global ``exclude.txt`` plus a per-destination
        ``exclude-<name>.txt`` in the config directory; the optional
        ``exclude_files`` config key replaces the defaults entirely.
        Missing files are ignored by the backends.
        """
        if self._exclude_files_override is not None:
            return [Path(p).expanduser() for p in self._exclude_files_override]
        cfg_dir = self._config_dir or Config._default_dir()
        return [
            cfg_dir / "exclude.txt",
            cfg_dir / f"exclude-{self.name}.txt",
        ]

    @property
    def per_source_include_files(self) -> list[list[Path]]:
        """Include-list files per source (empty list = no include filtering)."""
        return [[Path(p).expanduser() for p in lst] for lst in self._per_source_includes]

    @property
    def per_source_exclude_files(self) -> list[list[Path] | None]:
        """Exclusion files per source (None = use destination-level defaults)."""
        return [
            None if lst is None else [Path(p).expanduser() for p in lst]
            for lst in self._per_source_excludes
        ]

    def __repr__(self) -> str:
        return f"DestinationConfig(name={self.name!r}, method={self.method!r})"


class Config:
    """Represents the full bu configuration — a directory of .toml files.

    Each .toml file in the config directory represents one destination.
    The filename (minus .toml) is the destination name.
    """

    def __init__(self, config_dir: Path | None = None) -> None:
        self.config_dir = config_dir or self._default_dir()
        self.destinations: dict[str, DestinationConfig] = {}

        if self.config_dir.exists():
            self._load()

    @staticmethod
    def _default_dir() -> Path:
        """Return the default config directory path.

        Uses $BU_CONFIG_DIR if set, otherwise ~/.config/bu/.
        """
        if env_dir := os.environ.get("BU_CONFIG_DIR"):
            return Path(env_dir)
        xdg = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
        return Path(xdg) / "bu"

    def _load(self) -> None:
        """Scan the config directory for *.toml files and parse each one."""
        if not self.config_dir.is_dir():
            raise ConfigError(f"Config path is not a directory: {self.config_dir}")

        toml_files = sorted(self.config_dir.glob("*.toml"))
        if not toml_files:
            return  # No destinations configured yet — not an error

        errors: list[str] = []
        for fp in toml_files:
            name = fp.stem  # filename without .toml
            try:
                with open(fp, "rb") as f:
                    data = tomllib.load(f)
            except tomllib.TOMLDecodeError as e:
                errors.append(f"{fp.name}: invalid TOML — {e}")
                continue
            except OSError as e:
                errors.append(f"{fp.name}: cannot read — {e}")
                continue

            if not isinstance(data, dict):
                errors.append(f"{fp.name}: must contain key-value pairs")
                continue

            method = data.get("method", "")
            if not method:
                errors.append(f"{fp.name}: missing required 'method' key (must be 'rsync', 'duplicity' or 'snapshot')")
                continue

            if method not in _VALID_METHODS:
                errors.append(f"{fp.name}: unknown method {method!r} — must be 'rsync', 'duplicity' or 'snapshot'")
                continue

            source_paths = data.get("source_paths", [])
            if not source_paths:
                errors.append(f"{fp.name}: missing required 'source_paths'")
                continue

            destination = data.get("destination", "")
            if not destination:
                errors.append(f"{fp.name}: missing required 'destination' path")
                continue

            try:
                self.destinations[name] = DestinationConfig(
                    name, data, config_dir=self.config_dir
                )
            except ConfigError as e:
                errors.append(f"{fp.name}: {e}")

        if errors:
            raise ConfigError("\n".join(errors))

    def get(self, name: str) -> DestinationConfig:
        """Return configuration for a named destination.

        Raises ConfigError if the name is invalid or not found.
        """
        if not VALID_NAME_RE.match(name):
            raise ConfigError(
                f"Invalid destination name {name!r} — names may only contain "
                "letters, digits, '-' and '_'."
            )
        if name not in self.destinations:
            available = ", ".join(sorted(self.destinations.keys())) or "(none)"
            raise ConfigError(
                f"Unknown destination {name!r}. "
                f"Available destinations: {available}"
            )
        return self.destinations[name]

    def list_destinations(self) -> list[str]:
        """Return a sorted list of configured destination names."""
        return sorted(self.destinations.keys())

    def destination_path(self, name: str) -> Path:
        """Return the expected .toml file path for a destination name."""
        return self.config_dir / f"{name}.toml"

