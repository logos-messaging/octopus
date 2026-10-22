import time

import httpx
import pytest
from _oidc_keys import AUDIENCE, FakeIssuer
from cryptography.hazmat.primitives.asymmetric import rsa

from octopus.oidc import OidcError, OidcVerifier


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def issuer():
    return FakeIssuer()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def verifier(issuer, clock):
    return OidcVerifier(AUDIENCE, fetch=issuer.jwks, clock=clock)


def test_valid_token(verifier, issuer):
    claims = verifier.verify(issuer.token())
    assert claims.repository == "acme/app"
    assert claims.actor == "alice"
    assert claims.event_name == "push"
    assert claims.run_id == "1001"


def test_expired(verifier, issuer):
    now = int(time.time())
    with pytest.raises(OidcError, match="expired"):
        verifier.verify(issuer.token(iat=now - 900, nbf=now - 900, exp=now - 600))


def test_not_yet_valid(verifier, issuer):
    now = int(time.time())
    with pytest.raises(OidcError):
        verifier.verify(issuer.token(nbf=now + 600, exp=now + 900))


def test_wrong_audience(verifier, issuer):
    with pytest.raises(OidcError, match="audience"):
        verifier.verify(issuer.token(aud="https://other.example.org"))


def test_wrong_issuer(verifier, issuer):
    with pytest.raises(OidcError, match="issuer"):
        verifier.verify(issuer.token(iss="https://evil.example.org"))


def test_bad_signature(verifier, issuer):
    forged = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(OidcError, match="invalid"):
        verifier.verify(issuer.token(key=forged))


def test_replayed_jti(verifier, issuer):
    token = issuer.token(jti="same")
    verifier.verify(token)
    with pytest.raises(OidcError, match="already used"):
        verifier.verify(token)


def test_jti_forgotten_after_expiry(verifier, issuer, monkeypatch):
    verifier.verify(issuer.token(jti="a"))
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 1000)
    verifier._claim_jti(type("C", (), {"jti": "b", "exp": real() + 1500})())
    assert "a" not in verifier._seen


def test_alg_none_and_hs256_rejected(verifier):
    import jwt

    for alg in ("HS256",):
        token = jwt.encode({"iss": "x"}, "secret" * 8, algorithm=alg, headers={"kid": "k1"})
        with pytest.raises(OidcError, match="algorithm"):
            verifier.verify(token)
    with pytest.raises(OidcError):
        verifier.verify("not.a.jwt")


def test_missing_claim(verifier, issuer):
    with pytest.raises(OidcError):
        verifier.verify(issuer.token(repository=None))


def test_unknown_kid_refreshes_keys(verifier, issuer, clock):
    verifier.verify(issuer.token())
    assert issuer.fetches == 1
    new = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    issuer.publish("k2", new)
    clock.now += 60
    verifier.verify(issuer.token(key=new, kid="k2"))
    assert issuer.fetches == 2


def test_unknown_kid_refresh_is_rate_limited(verifier, issuer):
    verifier.verify(issuer.token())
    for _ in range(5):
        with pytest.raises(OidcError, match="unknown signing key"):
            verifier.verify(issuer.token(kid="nope"))
    assert issuer.fetches == 1


def test_unfetchable_keys(clock, issuer):
    def boom():
        raise httpx.ConnectError("down")

    verifier = OidcVerifier(AUDIENCE, fetch=boom, clock=clock)
    with pytest.raises(OidcError, match="could not fetch"):
        verifier.verify(issuer.token())
