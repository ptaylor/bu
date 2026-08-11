"""Abstract base class for all backup backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class Backend(ABC):
    """Abstract interface that all backup backends must implement."""

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialise the backend with its configuration dictionary.

        The config dict contains the ``destination`` path plus any extra
        keys from the TOML destination entry (excluding reserved keys).
        Internal keys ``_name`` and ``_source_paths`` are also provided.
        """
        self.config = config

    @abstractmethod
    def backup(
        self,
        source_paths: list[str],
        *,
        dry_run: bool = False,
        extra_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Back up the given source paths to this backend.

        Returns a dict with summary information (files_copied, bytes_copied, etc.).
        """
        ...

    @abstractmethod
    def restore(
        self,
        restore_path: str,
        *,
        dry_run: bool = False,
        extra_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Restore data from this backend to restore_path.

        Returns a dict with summary information.
        """
        ...

    @abstractmethod
    def check(
        self,
        source_paths: list[str],
        *,
        extra_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Check what would be backed up (diff between source and destination).

        Returns a dict with files_to_backup, files_to_update, total_size, etc.
        """
        ...

    @abstractmethod
    def verify(
        self,
        source_paths: list[str],
        *,
        full: bool = False,
        extra_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Verify integrity of backed-up data.

        If full=True, perform a full checksum comparison.
        Returns a dict with verified count, errors, etc.
        """
        ...

    @abstractmethod
    def status(
        self,
        *,
        extra_args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return status information about the backup destination.

        Returns a dict with last_backup, total_size, file_count, etc.
        """
        ...
