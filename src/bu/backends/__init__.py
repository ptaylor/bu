"""Backup method plugins for bu.

Each method module provides a class implementing the Backend interface.
"""

from bu.backends.base import Backend
from bu.backends.duplicity import DuplicityMethod
from bu.backends.local import RsyncMethod
from bu.backends.snapshot import SnapshotMethod

__all__ = ["Backend", "DuplicityMethod", "RsyncMethod", "SnapshotMethod"]

# Registry of method name -> class
REGISTRY: dict[str, type[Backend]] = {
    "rsync": RsyncMethod,
    "duplicity": DuplicityMethod,
    "snapshot": SnapshotMethod,
}


def get_backend(name: str) -> type[Backend]:
    """Look up a backend class by method name."""
    try:
        return REGISTRY[name]
    except KeyError:
        available = ", ".join(sorted(REGISTRY.keys()))
        raise ValueError(
            f"Unknown method {name!r}. Available methods: {available}"
        ) from None
