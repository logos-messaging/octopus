from __future__ import annotations

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey

from ..crypto import KeyMaterialError, decode_public_key, encode_public_key, node_id
from ..crypto.signing import decode_signing_public, encode_signing_public, sign, verify
from ..proto.adverts import ADVERT_VERSION, Advert, CoordinatorCert

# Longest an advert may claim to be valid, so a captured one cannot be replayed for long.
MAX_ADVERT_TTL_S = 300.0
# How far a coordinator's clock may run ahead of ours.
MAX_CLOCK_SKEW_S = 60.0

MALFORMED = "malformed"
UNSUPPORTED_VERSION = "unsupported-version"
CERT_EXPIRED = "cert-expired"
BAD_ROOT_SIGNATURE = "bad-root-signature"
ID_MISMATCH = "id-mismatch"
BAD_SIGNATURE = "bad-signature"
EXPIRED = "expired"
FROM_FUTURE = "from-future"
REPLAYED = "replayed"
REVOKED = "revoked"
RATE_LIMITED = "rate-limited"


class AdvertError(ValueError):
    """An advert was rejected; `reason` is one of the constants above."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


def issue_cert(
    root_key: Ed25519PrivateKey,
    enc_pub: X25519PublicKey,
    sign_pub: Ed25519PublicKey,
    not_after: float,
) -> CoordinatorCert:
    """Run by whoever holds the root key (offline): authorise a coordinator until `not_after`."""
    cert = CoordinatorCert(
        coordinator_id=node_id(enc_pub),
        enc_pub=encode_public_key(enc_pub),
        sign_pub=encode_signing_public(sign_pub),
        not_after=not_after,
    )
    return cert.model_copy(update={"root_sig": sign(root_key, cert.signing_bytes())})


def make_advert(
    cert: CoordinatorCert,
    sign_key: Ed25519PrivateKey,
    *,
    multiaddrs: list[str],
    capabilities: list[str],
    now: float,
    ttl_s: float,
    software: str = "",
) -> Advert:
    advert = Advert(
        cert=cert,
        multiaddrs=multiaddrs,
        capabilities=capabilities,
        software=software,
        sent_at=now,
        expires_at=now + min(ttl_s, MAX_ADVERT_TTL_S),
    )
    return advert.model_copy(update={"sig": sign(sign_key, advert.signing_bytes())})


def verify_advert(advert: Advert, root_pub: Ed25519PublicKey, now: float) -> None:
    """Raise AdvertError unless the advert is signed by a coordinator the root key authorised."""
    if advert.version != ADVERT_VERSION:
        raise AdvertError(UNSUPPORTED_VERSION, str(advert.version))
    cert = advert.cert
    if cert.not_after <= now:
        raise AdvertError(CERT_EXPIRED, cert.coordinator_id)
    if not verify(root_pub, cert.signing_bytes(), cert.root_sig):
        raise AdvertError(BAD_ROOT_SIGNATURE, cert.coordinator_id)
    try:
        enc_pub = decode_public_key(cert.enc_pub)
        sign_pub = decode_signing_public(cert.sign_pub)
    except KeyMaterialError as e:
        raise AdvertError(MALFORMED, f"could not parse cert keys: {e}") from e
    if node_id(enc_pub) != cert.coordinator_id:
        raise AdvertError(ID_MISMATCH, cert.coordinator_id)
    if not verify(sign_pub, advert.signing_bytes(), advert.sig):
        raise AdvertError(BAD_SIGNATURE, cert.coordinator_id)
    if advert.expires_at <= now:
        raise AdvertError(EXPIRED, cert.coordinator_id)
    if advert.expires_at - advert.sent_at > MAX_ADVERT_TTL_S:
        raise AdvertError(MALFORMED, f"ttl too long: {cert.coordinator_id}")
    if advert.sent_at > now + MAX_CLOCK_SKEW_S:
        raise AdvertError(FROM_FUTURE, cert.coordinator_id)
