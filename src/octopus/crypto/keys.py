from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_RAW = serialization.Encoding.Raw
_RAW_PUB = serialization.PublicFormat.Raw
_RAW_PRIV = serialization.PrivateFormat.Raw
_NO_ENC = serialization.NoEncryption()


class KeyMaterialError(ValueError):
    """Raised for malformed key material. Never carries the key itself."""


def generate_private_key() -> X25519PrivateKey:
    return X25519PrivateKey.generate()


def encode_public_key(key: X25519PublicKey) -> str:
    return base64.urlsafe_b64encode(key.public_bytes(_RAW, _RAW_PUB)).decode().rstrip("=")


def decode_public_key(text: str) -> X25519PublicKey:
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        return X25519PublicKey.from_public_bytes(raw)
    except (ValueError, TypeError) as exc:
        raise KeyMaterialError("invalid public key") from exc


def node_id(pub: X25519PublicKey) -> str:
    """Stable id bound to a public key, so a forged presence cannot claim another node's id."""
    digest = hashlib.sha256(pub.public_bytes(_RAW, _RAW_PUB)).digest()
    return base64.b32encode(digest).decode().rstrip("=").lower()[:16]


def save_private_key(key: X25519PrivateKey, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key.private_bytes(_RAW, _RAW_PRIV, _NO_ENC))


def load_private_key(path: Path) -> X25519PrivateKey:
    try:
        return X25519PrivateKey.from_private_bytes(path.read_bytes())
    except ValueError as exc:
        raise KeyMaterialError("invalid private key file") from exc


def load_or_create_private_key(path: Path) -> X25519PrivateKey:
    if path.exists():
        return load_private_key(path)
    key = generate_private_key()
    save_private_key(key, path)
    return key


def derive_channel_key(own: X25519PrivateKey, peer: X25519PublicKey, channel_id: str) -> bytes:
    shared = own.exchange(peer)
    return HKDF(
        algorithm=SHA256(), length=32, salt=None, info=b"octopus/1/channel/" + channel_id.encode()
    ).derive(shared)
