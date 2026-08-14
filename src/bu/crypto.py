"""Secret encryption helpers.

Uses GPG symmetric encryption (``gpg --symmetric``) so no extra Python
dependencies are required — GPG is already a hard requirement of the
duplicity backup method.

Encrypted blobs are ASCII-armored and safe to paste into a .toml config.
"""

from __future__ import annotations

import subprocess


class CryptoError(Exception):
    """Raised when encryption/decryption fails."""


def _run_gpg(args: list[str], stdin_text: str) -> str:
    """Run gpg with the given args, feeding stdin_text on stdin."""
    try:
        proc = subprocess.run(
            ["gpg", "--batch", "--yes", "--pinentry-mode", "loopback", *args],
            input=stdin_text,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as e:
        raise CryptoError("gpg binary not found. Is GPG installed?") from e

    if proc.returncode != 0:
        raise CryptoError(proc.stderr.strip() or f"gpg failed with exit code {proc.returncode}")
    return proc.stdout


def encrypt_secret(plaintext: str, password: str) -> str:
    """Encrypt a secret with GPG symmetric encryption.  Returns armored text."""
    out = _run_gpg(
        ["--symmetric", "--armor", "--passphrase", password],
        plaintext,
    )
    return out.strip()


def decrypt_secret(armored: str, password: str) -> str:
    """Decrypt an armored GPG symmetric blob.  Returns the plaintext secret."""
    out = _run_gpg(
        ["--decrypt", "--passphrase", password],
        armored,
    )
    return out.rstrip("\n")
