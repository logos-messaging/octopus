from datetime import datetime, timezone

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from octopus.github.app import JWT_LIFETIME_S, GitHubApiError, GitHubApp, repo_path

T0 = 1_700_000_000.0


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGitHub:
    """Minimal GitHub API: issues installation tokens and serves one authenticated endpoint."""

    def __init__(self, clock, token_status=201):
        self.clock = clock
        self.token_status = token_status
        self.issued = []
        self.requests = []
        self.revoked = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        auth = request.headers.get("Authorization", "")
        if request.url.path.endswith("/access_tokens"):
            if self.token_status != 201:
                return httpx.Response(self.token_status, json={"message": "Bad credentials"})
            token = f"ghs_token{len(self.issued)}"
            self.issued.append(token)
            return httpx.Response(
                201, json={"token": token, "expires_at": iso(self.clock() + 3600)}
            )
        if request.url.path == "/repos/acme/app/installation":
            return httpx.Response(200, json={"id": 77})
        if request.url.path == "/repos/acme/app":
            token = auth.removeprefix("Bearer ")
            if token not in self.issued or token in self.revoked:
                return httpx.Response(401, json={"message": "Bad credentials"})
            return httpx.Response(200, json={"full_name": "acme/app"})
        return httpx.Response(404, json={"message": "Not Found"})


@pytest.fixture
def clock():
    now = [T0]

    def tick(dt=0.0):
        now[0] += dt
        return now[0]

    return tick


@pytest.fixture
def gh(clock):
    return FakeGitHub(clock)


@pytest.fixture
def app(rsa_key_pem, gh, clock):
    client = httpx.Client(transport=httpx.MockTransport(gh.handler))
    return GitHubApp(12345, rsa_key_pem, client=client, clock=clock)


def public_key(pem):
    return load_pem_private_key(pem, password=None).public_key()


def test_app_jwt_claims(app, rsa_key_pem):
    token = app.app_jwt()
    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    claims = jwt.decode(
        token, public_key(rsa_key_pem), algorithms=["RS256"], options={"verify_exp": False}
    )
    assert claims["iss"] == "12345"
    assert claims["iat"] == int(T0) - 60  # backdated for clock drift
    assert claims["exp"] == int(T0) + JWT_LIFETIME_S
    assert claims["exp"] - claims["iat"] <= 600  # GitHub's maximum


def test_app_jwt_is_reused_until_near_expiry(app, clock):
    first = app.app_jwt()
    clock(JWT_LIFETIME_S - 120)
    assert app.app_jwt() == first
    clock(90)
    assert app.app_jwt() != first


def test_installation_token_created_with_app_jwt(app, gh, rsa_key_pem):
    assert app.installation_token(42) == "ghs_token0"
    req = gh.requests[-1]
    assert req.method == "POST" and req.url.path == "/app/installations/42/access_tokens"
    bearer = req.headers["Authorization"].removeprefix("Bearer ")
    jwt.decode(bearer, public_key(rsa_key_pem), algorithms=["RS256"], options={"verify_exp": False})
    assert req.headers["X-GitHub-Api-Version"] == "2022-11-28"
    assert req.headers["Accept"] == "application/vnd.github+json"


def test_installation_token_cached_and_refreshed(app, gh, clock):
    assert app.installation_token(42) == "ghs_token0"
    clock(3600 - 6 * 60)
    assert app.installation_token(42) == "ghs_token0"
    assert len(gh.issued) == 1
    clock(2 * 60)  # inside the refresh margin
    assert app.installation_token(42) == "ghs_token1"


def test_installation_tokens_are_per_installation(app):
    assert app.installation_token(1) != app.installation_token(2)
    assert app.installation_token(1) == "ghs_token0"


def test_request_uses_installation_token(app, gh):
    assert app.get_json("/repos/acme/app", 42) == {"full_name": "acme/app"}
    assert gh.requests[-1].headers["Authorization"] == "Bearer ghs_token0"


def test_request_refreshes_revoked_token_once(app, gh):
    app.installation_token(42)
    gh.revoked.add("ghs_token0")
    assert app.request("GET", "/repos/acme/app", 42).status_code == 200
    assert gh.requests[-1].headers["Authorization"] == "Bearer ghs_token1"


def test_token_error_raises_without_leaking_credentials(rsa_key_pem, clock):
    gh = FakeGitHub(clock, token_status=401)
    client = httpx.Client(transport=httpx.MockTransport(gh.handler))
    app = GitHubApp(12345, rsa_key_pem, client=client, clock=clock)
    with pytest.raises(GitHubApiError) as exc:
        app.installation_token(42)
    assert exc.value.status == 401
    assert "Bad credentials" in str(exc.value)
    assert app.app_jwt() not in str(exc.value)


def test_malformed_token_response(rsa_key_pem, clock):
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(201, text="<html>"))
    )
    app = GitHubApp(12345, rsa_key_pem, client=client, clock=clock)
    with pytest.raises(GitHubApiError, match="malformed token response"):
        app.installation_token(42)


def test_get_json_error(app):
    with pytest.raises(GitHubApiError) as exc:
        app.get_json("/repos/acme/missing", 42)
    assert exc.value.status == 404


def test_repo_installation_id(app, gh):
    assert app.repo_installation_id("acme/app") == 77
    assert gh.requests[-1].headers["Authorization"].startswith("Bearer ey")  # the app JWT


def test_secrets_not_in_repr(app, rsa_key_pem):
    token = app.installation_token(42)
    text = repr(app) + repr(app._tokens)
    assert token not in text
    assert rsa_key_pem.decode().splitlines()[1] not in text


def test_from_key_file(tmp_path, rsa_key_pem):
    key = tmp_path / "app.pem"
    key.write_bytes(rsa_key_pem)
    assert GitHubApp.from_key_file(1, key).app_jwt().count(".") == 2


@pytest.mark.parametrize(
    ("repo", "path"),
    [
        ("acme/app", "/repos/acme/app"),
        ("a/b.c-d_e", "/repos/a/b.c-d_e"),
        ("a/../x", "/repos/a/..%2Fx"),
    ],
)
def test_repo_path(repo, path):
    assert repo_path(repo) == path


@pytest.mark.parametrize("repo", ["acme", "/app", "acme/"])
def test_repo_path_rejects_bad_names(repo):
    with pytest.raises(ValueError):
        repo_path(repo)
