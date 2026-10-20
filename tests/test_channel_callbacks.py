
from octopus.crypto import PairCipher, derive_channel_key, generate_private_key
from octopus.transport.logos import _Channel


def _call(channel, fn_attr, data: bytes):
    """Invoke the C callback the way the library does."""
    ffi = channel._ffi
    cb = getattr(channel, fn_attr)
    out = ffi.new("const uint8_t **")
    out_len = ffi.new("size_t *")
    inp = ffi.from_buffer("uint8_t[]", data) if data else ffi.NULL
    rc = cb(ffi.NULL, inp, len(data), out, out_len)
    return rc, ffi.buffer(out[0], out_len[0])[:] if rc == 0 else None


def _channel():
    a, b = generate_private_key(), generate_private_key()
    key = derive_channel_key(a, b.public_key(), "c")
    return _Channel("c", "t", "me", PairCipher(key), lambda s, p: None)


def test_encrypt_then_decrypt_through_c_callbacks():
    ch = _channel()
    rc, ct = _call(ch, "_enc", b"hello")
    assert rc == 0 and b"hello" not in ct
    rc, pt = _call(ch, "_dec", ct)
    assert rc == 0 and pt == b"hello"


def test_decrypt_failure_returns_nonzero_instead_of_raising():
    rc, out = _call(_channel(), "_dec", b"x" * 40)
    assert rc != 0 and out is None


def test_callback_addresses_are_distinct_per_direction():
    ch = _channel()
    ffi = ch._ffi
    assert int(ffi.cast("uintptr_t", ch._enc)) != int(ffi.cast("uintptr_t", ch._dec))


def test_ciphertext_has_no_trailing_byte():
    _, ct = _call(_channel(), "_enc", b"hello")
    assert len(ct) == 12 + len(b"hello") + 16
