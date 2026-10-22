"""SSH signatures (`ssh-keygen -Y sign`, PROTOCOL.sshsig), Ed25519 keys only.

A node proves that it belongs to a GitHub user by signing its keys with that user's SSH key;
GitHub publishes the public keys of every user at https://github.com/<login>.keys.
"""

from __future__ import annotations

import base64
import hashlib
import struct
import subprocess
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

MAGIC = b"SSHSIG"
VERSION = 1
KEY_TYPE = b"ssh-ed25519"
BEGIN = "-----BEGIN SSH SIGNATURE-----"
END = "-----END SSH SIGNATURE-----"
_HASHES = {"sha256": hashlib.sha256, "sha512": hashlib.sha512}


class SshSigError(ValueError):
    """The signature is malformed or does not verify."""


def _string(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


class _Reader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def take(self, n: int) -> bytes:
        if n < 0 or self._pos + n > len(self._data):
            raise SshSigError("truncated signature")
        chunk = self._data[self._pos : self._pos + n]
        self._pos += n
        return chunk

    def uint32(self) -> int:
        return int(struct.unpack(">I", self.take(4))[0])

    def string(self) -> bytes:
        return self.take(self.uint32())

    def done(self) -> bool:
        return self._pos == len(self._data)


def _raw_public(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _signed_blob(namespace: str, hash_alg: str, message: bytes) -> bytes:
    digest = _HASHES[hash_alg](message).digest()
    return (
        MAGIC
        + _string(namespace.encode())
        + _string(b"")
        + _string(hash_alg.encode())
        + _string(digest)
    )


def _armor(blob: bytes) -> str:
    body = base64.b64encode(blob).decode()
    lines = [body[i : i + 70] for i in range(0, len(body), 70)]
    return "\n".join([BEGIN, *lines, END]) + "\n"


def build_sshsig(key: Ed25519PrivateKey, namespace: str, message: bytes) -> str:
    """What `ssh-keygen -Y sign -n <namespace>` would print for an Ed25519 key."""
    hash_alg = "sha512"
    sig = key.sign(_signed_blob(namespace, hash_alg, message))
    blob = (
        MAGIC
        + struct.pack(">I", VERSION)
        + _string(_string(KEY_TYPE) + _string(_raw_public(key.public_key())))
        + _string(namespace.encode())
        + _string(b"")
        + _string(hash_alg.encode())
        + _string(_string(KEY_TYPE) + _string(sig))
    )
    return _armor(blob)


def verify_sshsig(armored: str, namespace: str, message: bytes) -> bytes:
    """The raw public key that signed `message` in `namespace`. Raises SshSigError."""
    lines = [ln.strip() for ln in armored.strip().splitlines()]
    if len(lines) < 3 or lines[0] != BEGIN or lines[-1] != END:
        raise SshSigError("not an SSH signature")
    try:
        blob = base64.b64decode("".join(lines[1:-1]), validate=True)
    except ValueError:
        raise SshSigError("bad base64 in signature") from None
    r = _Reader(blob)
    if r.take(len(MAGIC)) != MAGIC or r.uint32() != VERSION:
        raise SshSigError("unsupported signature version")
    pub = _Reader(r.string())
    if pub.string() != KEY_TYPE:
        raise SshSigError("only ssh-ed25519 keys are supported")
    raw_pub = pub.string()
    if r.string() != namespace.encode():
        raise SshSigError("wrong namespace")
    r.string()  # reserved
    hash_alg = r.string().decode(errors="replace")
    if hash_alg not in _HASHES:
        raise SshSigError("unsupported hash")
    sig = _Reader(r.string())
    if sig.string() != KEY_TYPE:
        raise SshSigError("signature key type mismatch")
    raw_sig = sig.string()
    if not r.done():
        raise SshSigError("trailing data in signature")
    try:
        Ed25519PublicKey.from_public_bytes(raw_pub).verify(
            raw_sig, _signed_blob(namespace, hash_alg, message)
        )
    except (InvalidSignature, ValueError):
        raise SshSigError("signature does not verify") from None
    return raw_pub


def parse_authorized_keys(text: str) -> list[bytes]:
    """Raw Ed25519 public keys from an authorized_keys style list (other key types are skipped)."""
    keys: list[bytes] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] != KEY_TYPE.decode():
            continue
        try:
            r = _Reader(base64.b64decode(parts[1], validate=True))
            if r.string() == KEY_TYPE:
                keys.append(r.string())
        except (ValueError, SshSigError):
            continue
    return keys


def ssh_keygen_sign(message: bytes, key_file: Path, namespace: str) -> str:
    """Sign with the user's own key through `ssh-keygen` (it uses the agent when it holds the key)."""
    try:
        proc = subprocess.run(
            ["ssh-keygen", "-Y", "sign", "-f", str(key_file), "-n", namespace],
            input=message,
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise SshSigError(f"could not run ssh-keygen: {e}") from e
    if proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip()
        raise SshSigError(f"ssh-keygen could not sign: exit code: {proc.returncode}: {detail}")
    return proc.stdout.decode()
