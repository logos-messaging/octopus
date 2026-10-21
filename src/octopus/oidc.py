"""Verification of GitHub Actions OIDC tokens, with no stored credential.

A workflow job with `permissions: id-token: write` can ask GitHub for a short-lived JWT
signed with a key published at `JWKS_URL`. Checking it needs only that public key set, so the
coordinator holds no GitHub secret. A token is accepted once: its `jti` is remembered until
the token expires.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm

log = logging.getLogger(__name__)

ISSUER = "https://token.actions.githubusercontent.com"
JWKS_URL = f"{ISSUER}/.well-known/jwks"
LEEWAY_S = 30
# An unknown `kid` refreshes the key set, but not more often than this, so a flood of forged
# tokens cannot turn into a flood of requests to GitHub.
MIN_REFRESH_S = 30.0
FETCH_TIMEOUT_S = 10.0
_REQUIRED = ("exp", "iat", "iss", "aud", "jti", "repository")

JwksFetcher = Callable[[], dict]


class OidcError(Exception):
    """The token is not acceptable. The message is safe to send to the caller."""


@dataclass(frozen=True)
class Claims:
    jti: str
    repository: str
    repository_owner: str
    actor: str
    event_name: str
    ref: str
    job_workflow_ref: str
    run_id: str
    run_attempt: str
    exp: float


def fetch_jwks(url: str = JWKS_URL) -> dict:
    resp = httpx.get(url, timeout=FETCH_TIMEOUT_S)
    resp.raise_for_status()
    return resp.json()


class OidcVerifier:
    def __init__(
        self,
        audience: str,
        *,
        issuer: str = ISSUER,
        fetch: JwksFetcher = fetch_jwks,
        clock: Callable[[], float] = time.monotonic,
        min_refresh_s: float = MIN_REFRESH_S,
    ) -> None:
        if not audience:
            raise ValueError("an audience is required")
        self._audience = audience
        self._issuer = issuer
        self._fetch = fetch
        self._clock = clock
        self._min_refresh_s = min_refresh_s
        self._lock = threading.Lock()
        self._keys: dict[str, Any] = {}
        self._fetched_at: float | None = None
        self._seen: dict[str, float] = {}  # jti -> exp (epoch seconds)

    def verify(self, token: str) -> Claims:
        """The claims of a valid, unused token. Raises OidcError."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise OidcError("malformed token") from None
        if header.get("alg") != "RS256":
            raise OidcError("unsupported token algorithm")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise OidcError("token has no key id")
        key = self._key(kid)
        try:
            raw = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                leeway=LEEWAY_S,
                options={"require": list(_REQUIRED)},
            )
        except jwt.ExpiredSignatureError:
            raise OidcError("token expired") from None
        except jwt.InvalidAudienceError:
            raise OidcError("wrong audience") from None
        except jwt.InvalidIssuerError:
            raise OidcError("wrong issuer") from None
        except jwt.PyJWTError:
            raise OidcError("invalid token") from None
        claims = Claims(
            jti=str(raw["jti"]),
            repository=str(raw["repository"]),
            repository_owner=str(raw.get("repository_owner", "")),
            actor=str(raw.get("actor", "")),
            event_name=str(raw.get("event_name", "")),
            ref=str(raw.get("ref", "")),
            job_workflow_ref=str(raw.get("job_workflow_ref", "")),
            run_id=str(raw.get("run_id", "")),
            run_attempt=str(raw.get("run_attempt", "1")),
            exp=float(raw["exp"]),
        )
        self._claim_jti(claims)
        return claims

    def _claim_jti(self, claims: Claims) -> None:
        now = time.time()
        with self._lock:
            for jti in [j for j, exp in self._seen.items() if exp + LEEWAY_S < now]:
                del self._seen[jti]
            if claims.jti in self._seen:
                raise OidcError("token already used")
            self._seen[claims.jti] = claims.exp

    def _key(self, kid: str) -> Any:
        with self._lock:
            key = self._keys.get(kid)
            if key is not None:
                return key
            now = self._clock()
            if self._fetched_at is not None and now - self._fetched_at < self._min_refresh_s:
                raise OidcError("unknown signing key")
            try:
                jwks = self._fetch()
            except (httpx.HTTPError, ValueError) as e:
                log.warning("could not fetch the GitHub OIDC keys: %s", e)
                raise OidcError("could not fetch the signing keys") from None
            self._fetched_at = now
            self._keys = _parse_jwks(jwks)
            key = self._keys.get(kid)
        if key is None:
            raise OidcError("unknown signing key")
        return key


def _parse_jwks(jwks: dict) -> dict[str, Any]:
    keys: dict[str, Any] = {}
    for jwk in jwks.get("keys", []) if isinstance(jwks, dict) else []:
        if not isinstance(jwk, dict) or jwk.get("kty") != "RSA" or not jwk.get("kid"):
            continue
        try:
            keys[str(jwk["kid"])] = RSAAlgorithm.from_jwk(jwk)
        except (jwt.PyJWTError, ValueError):
            continue
    return keys
