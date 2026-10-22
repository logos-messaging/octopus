"""The team signing key on disk: encrypted with a passphrase, loaded only for a moment."""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

PASSPHRASE_ENV = "OCTOPUS_TEAM_PASSPHRASE"


class KeyFileError(ValueError):
    """The key file could not be read; the message never contains the key or the passphrase."""


def save_team_key(key: Ed25519PrivateKey, path: Path, passphrase: str) -> None:
    if not passphrase:
        raise KeyFileError("a passphrase is required")
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(passphrase.encode()),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)


def load_team_key(path: Path, passphrase: str) -> Ed25519PrivateKey:
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), passphrase.encode())
    except FileNotFoundError:
        raise KeyFileError(f"no team key: {path}") from None
    except (ValueError, TypeError):
        raise KeyFileError("wrong passphrase or unreadable team key") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise KeyFileError("the team key is not an Ed25519 key")
    return key
