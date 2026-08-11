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

    def restore(self, restore_path, *, dry_run=False, extra_args=None):
        return {"files_restored": 0, "bytes_restored": 0, "errors": ["duplicity method is not yet implemented"]}

    def check(self, source_paths, *, extra_args=None):
        return {"files_to_backup": 0, "files_to_update": 0, "total_size": 0, "errors": ["duplicity method is not yet implemented"]}

    def verify(self, source_paths, *, full=False, extra_args=None):
        return {"verified": 0, "missing": [], "mismatched": [], "errors": ["duplicity method is not yet implemented"]}

    def status(self, *, extra_args=None):
        return {"exists": False, "file_count": 0, "total_size": 0, "last_backup": None}
