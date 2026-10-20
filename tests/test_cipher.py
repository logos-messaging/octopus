import pytest

from octopus.crypto import (
    CipherError,
    PairCipher,
    decode_public_key,
    derive_channel_key,
    encode_public_key,
    generate_private_key,
    load_or_create_private_key,
    node_id,
)
from octopus.crypto.keys import KeyMaterialError


def pair():
    a, b = generate_private_key(), generate_private_key()
    ka = derive_channel_key(a, b.public_key(), "chan")
    kb = derive_channel_key(b, a.public_key(), "chan")
    return ka, kb


def test_both_sides_derive_same_key():
    ka, kb = pair()
    assert ka == kb


def test_channel_id_separates_keys():
    a, b = generate_private_key(), generate_private_key()
    assert derive_channel_key(a, b.public_key(), "x") != derive_channel_key(a, b.public_key(), "y")


def test_round_trip_and_fresh_nonce():
    ka, kb = pair()
    c1, c2 = PairCipher(ka), PairCipher(kb)
    ct = c1.encrypt(b"secret")
    assert b"secret" not in ct
    assert c2.decrypt(ct) == b"secret"
    assert c1.encrypt(b"secret") != ct


def test_empty_payload():
    ka, kb = pair()
    assert PairCipher(kb).decrypt(PairCipher(ka).encrypt(b"")) == b""


def test_wrong_key_cannot_decrypt():
    ka, _ = pair()
    other = PairCipher(pair()[0])
    with pytest.raises(CipherError):
        other.decrypt(PairCipher(ka).encrypt(b"secret"))


def test_tampered_ciphertext_rejected():
    ka, kb = pair()
    ct = bytearray(PairCipher(ka).encrypt(b"secret"))
    ct[-1] ^= 1
    with pytest.raises(CipherError):
        PairCipher(kb).decrypt(bytes(ct))


def test_too_short_rejected():
    with pytest.raises(CipherError):
        PairCipher(pair()[0]).decrypt(b"short")


def test_public_key_round_trip_and_id_binding():
    pub = generate_private_key().public_key()
    assert decode_public_key(encode_public_key(pub)).public_bytes_raw() == pub.public_bytes_raw()
    assert node_id(pub) == node_id(decode_public_key(encode_public_key(pub)))


def test_bad_public_key_rejected_without_echoing_it():
    with pytest.raises(KeyMaterialError) as exc:
        decode_public_key("not-a-key")
    assert "not-a-key" not in str(exc.value)


def test_key_file_is_private_and_stable(tmp_path):
    path = tmp_path / "sub" / "key"
    k1 = load_or_create_private_key(path)
    assert path.stat().st_mode & 0o777 == 0o600
    k2 = load_or_create_private_key(path)
    assert node_id(k1.public_key()) == node_id(k2.public_key())
