"""Configuration loading and management for bu.

Configuration is read from ~/.config/bu/config.toml (or $BU_CONFIG_PATH if set).

Example config.toml:

    [destinations.photos]
    backend = "s3"
    source_paths = ["/home/user/photos"]
    bucket = "my-backups"
    region = "us-east-1"

    [destinations.docs]
    backend = "local"
    source_paths = ["/home/user/docs"]
    target_path = "/mnt/backup/docs"

    [destinations.server]
    backend = "rsync"
    source_paths = ["/home/user/data"]
    host = "backup.example.com"
    user = "paul"
    path = "/backups/data"
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


class ConfigError(Exception):
    """Raised when configuration is invalid or missing."""


class DestinationConfig:
    """Holds the parsed configuration for a single backup destination."""

    def __init__(self, name: str, data: dict[str, Any]) -> None:
        self.name = name
        self.backend: str = data.get("backend", "")
        self.source_paths: list[str] = data.get("source_paths", [])
        self.extra: dict[str, Any] = {
            k: v for k, v in data.items()
            if k not in ("backend", "source_paths")
        }

    def __repr__(self) -> str:
        return f"DestinationConfig(name={self.name!r}, backend={self.backend!r})"


class Config:
    """Represents the full bu configuration."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or self._default_path()
        self.destinations: dict[str, DestinationConfig] = {}
        self._raw: dict[str, Any] = {}

        if self.path.exists():
            self._load()

    @staticmethod
    def _default_path() -> Path:
        """Return the default config file path.

        Uses $BU_CONFIG_PATH if set, otherwise ~/.config/bu/config.toml.
        """
        if env_path := os.environ.get("BU_CONFIG_PATH"):
            return Path(env_path)
        # XDG_CONFIG_HOME or ~/.config
        xdg = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
        return Path(xdg) / "bu" / "config.toml"

    def _load(self) -> None:
        """Parse the TOML config file."""
        try:
            with open(self.path, "rb") as f:
                self._raw = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"Invalid TOML in {self.path}: {e}") from e
        except OSError as e:
            raise ConfigError(f"Cannot read {self.path}: {e}") from e

        # Parse [destinations] section
        dests = self._raw.get("destinations", {})
        if not dests:
            raise ConfigError(f"No [destinations] defined in {self.path}")

        for name, data in dests.items():
            if not isinstance(data, dict):
                raise ConfigError(
                    f"Destination [{name!r}] must be a table in {self.path}"
                )
            backend = data.get("backend", "")
            if not backend:
                raise ConfigError(
                    f"Destination [{name!r}] is missing required 'backend' key"
                )
            source_paths = data.get("source_paths", [])
            if not source_paths:
                raise ConfigError(
                    f"Destination [{name!r}] is missing required 'source_paths'"
                )
            self.destinations[name] = DestinationConfig(name, data)

    def get(self, name: str) -> DestinationConfig:
        """Return configuration for a named destination.

        Raises ConfigError if not found.
        """
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
