from __future__ import annotations

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

NONCE_LEN = 12


class CipherError(ValueError):
    """Decryption failed: wrong key or tampered data. Carries no plaintext."""


class PairCipher:
    """ChaCha20-Poly1305 under a pairwise key. Output is nonce || ciphertext || tag, so each
    segment decrypts on its own, as the logos-delivery channel cipher requires."""

    def __init__(self, key: bytes) -> None:
        self._aead = ChaCha20Poly1305(key)

    def encrypt(self, data: bytes) -> bytes:
        nonce = os.urandom(NONCE_LEN)
        return nonce + self._aead.encrypt(nonce, data, None)

    def decrypt(self, data: bytes) -> bytes:
        if len(data) < NONCE_LEN:
            raise CipherError("ciphertext too short")
        try:
            return self._aead.decrypt(data[:NONCE_LEN], data[NONCE_LEN:], None)
        except InvalidTag as exc:
            raise CipherError("decryption failed") from exc
