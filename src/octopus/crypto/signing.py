from __future__ import annotations

import base64
import os
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .keys import KeyMaterialError

_RAW = serialization.Encoding.Raw
_RAW_PUB = serialization.PublicFormat.Raw
_RAW_PRIV = serialization.PrivateFormat.Raw
_NO_ENC = serialization.NoEncryption()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def generate_signing_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def encode_signing_public(key: Ed25519PublicKey) -> str:
    return _b64e(key.public_bytes(_RAW, _RAW_PUB))


def decode_signing_public(text: str) -> Ed25519PublicKey:
    try:
        return Ed25519PublicKey.from_public_bytes(_b64d(text))
    except (ValueError, TypeError) as exc:
        raise KeyMaterialError("invalid signing public key") from exc


def sign(key: Ed25519PrivateKey, data: bytes) -> str:
    return _b64e(key.sign(data))


def verify(pub: Ed25519PublicKey, data: bytes, signature: str) -> bool:
    try:
        pub.verify(_b64d(signature), data)
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True


def save_signing_key(key: Ed25519PrivateKey, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key.private_bytes(_RAW, _RAW_PRIV, _NO_ENC))


def load_signing_key(path: Path) -> Ed25519PrivateKey:
    try:
        return Ed25519PrivateKey.from_private_bytes(path.read_bytes())
    except ValueError as exc:
        raise KeyMaterialError("invalid signing key file") from exc


def load_or_create_signing_key(path: Path) -> Ed25519PrivateKey:
    if path.exists():
        return load_signing_key(path)
    key = generate_signing_key()
    save_signing_key(key, path)
    return key
