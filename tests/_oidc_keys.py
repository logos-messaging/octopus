"""A fake GitHub OIDC issuer: generated RSA keys, a JWKS and signed tokens."""

import json
import time
import uuid

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

ISSUER = "https://token.actions.githubusercontent.com"
AUDIENCE = "https://octopus.example.org"


class FakeIssuer:
    def __init__(self, kid="k1"):
        self.kid = kid
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.fetches = 0
        self.published = {kid: self.key}

    def publish(self, kid, key):
        self.published[kid] = key

    def jwks(self):
        self.fetches += 1
        keys = []
        for kid, key in self.published.items():
            jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
            jwk.update(kid=kid, use="sig", alg="RS256")
            keys.append(jwk)
        return {"keys": keys}

    def token(self, key=None, kid=None, **overrides):
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": now,
            "nbf": now,
            "exp": now + 300,
            "jti": f"jti-{uuid.uuid4().hex}",
            "sub": "repo:acme/app:ref:refs/heads/main",
            "repository": "acme/app",
            "repository_owner": "acme",
            "actor": "alice",
            "event_name": "push",
            "ref": "refs/heads/main",
            "job_workflow_ref": "acme/app/.github/workflows/ci.yml@refs/heads/main",
            "run_id": "1001",
            "run_attempt": "1",
        }
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(
            claims, key or self.key, algorithm="RS256", headers={"kid": kid or self.kid}
        )
