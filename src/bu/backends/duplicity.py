"""duplicity method — backs up using the duplicity tool.

This is a stub.  Functionality will be added later.

Configuration keys required in the .toml file:
    destination: str — target directory for duplicity archives
"""

from __future__ import annotations

from typing import Any

from bu.backends.base import Backend


class DuplicityMethod(Backend):
    """Back up using duplicity.  (Stub — not yet implemented.)"""

    def _stub(self) -> dict[str, Any]:
        return {
            "files_copied": 0,
            "bytes_copied": 0,
            "errors": ["duplicity method is not yet implemented"],
        }

    def backup(self, source_paths, *, dry_run=False, extra_args=None):
        return self._stub()

    def restore(self, restore_path, path_within_backup=None, *, dry_run=False, extra_args=None):
        return {"files_restored": 0, "bytes_restored": 0, "errors": ["duplicity method is not yet implemented"]}

    def status(self, *, extra_args=None):
        return {
            "destination": self.config.get("_name", "unknown"),
            "method": "duplicity",
            "config_ok": True,
            "source_paths": self.config.get("_source_paths", []),
            "dest_path": self.config.get("destination", ""),
            "dest_exists": False,
            "last_backup": None,
        }
