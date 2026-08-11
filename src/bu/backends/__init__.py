"""Backend plugins for bu.

Each backend module provides a class implementing the Backend interface.
"""

from bu.backends.base import Backend
from bu.backends.local import LocalBackend
from bu.backends.rsync import RsyncBackend
from bu.backends.s3 import S3Backend

__all__ = ["Backend", "LocalBackend", "RsyncBackend", "S3Backend"]

# Registry of backend name -> class
REGISTRY: dict[str, type[Backend]] = {
    "local": LocalBackend,
    "rsync": RsyncBackend,
    "s3": S3Backend,
}


def get_backend(name: str) -> type[Backend]:
    """Look up a backend class by name."""
    try:
        return REGISTRY[name]
    except KeyError:
        available = ", ".join(sorted(REGISTRY.keys()))
        raise ValueError(
            f"Unknown backend {name!r}. Available backends: {available}"
        ) from None
