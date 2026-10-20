import httpx
import pytest
from _fake_github_repo import REPO, TOKEN, FakeRepoGitHub
from typer.testing import CliRunner

from octopus import cli_github
from octopus.cli import app
from octopus.github.setup import read_env_file

URL = "https://octopus.example.org/github/webhook"


@pytest.fixture
def gh(monkeypatch):
    fake = FakeRepoGitHub()
    monkeypatch.setattr(cli_github, "_client", lambda token, api_url: fake.client())
    monkeypatch.setattr(cli_github, "_interactive", lambda: False)
    monkeypatch.setattr("octopus.github.setup.PING_POLL_S", 0.0)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    return fake


@pytest.fixture
def env_file(tmp_path):
    return tmp_path / "coordinator.env"


def invoke(*args, input=None):
    return CliRunner().invoke(app, ["github", *args], input=input)


def setup_args(env_file, *extra):
    return [
        "setup",
        "--repo",
        REPO,
        "--webhook-url",
        URL,
        "--token",
        TOKEN,
        "--env-file",
        str(env_file),
        "--yes",
        *extra,
    ]


def test_setup_with_flags(gh, env_file):
    result = invoke(*setup_args(env_file))
    assert result.exit_code == 0, result.output
    assert "[changed] webhook" in result.output
    assert "[     ok] ping" in result.output
    env = read_env_file(env_file)
    assert env["OCTOPUS_GITHUB_TOKEN"] == TOKEN  # an explicit token is the coordinator's
    assert TOKEN not in result.output and env["OCTOPUS_WEBHOOK_SECRET"] not in result.output


def test_rerun_reads_repo_and_url_from_the_env_file(gh, env_file):
    assert invoke(*setup_args(env_file)).exit_code == 0
    gh.requests.clear()
    result = invoke("setup", "--token", TOKEN, "--env-file", str(env_file), "--yes")
    assert result.exit_code == 0, result.output
    assert "changed" not in result.output
    assert len(gh.mutations()) == 1  # the ping


def test_gh_login_token_is_not_written(gh, env_file, monkeypatch):
    def fake_gh(cmd, **kw):
        import subprocess

        return subprocess.CompletedProcess(cmd, 0, stdout=f"{TOKEN}\n", stderr="")

    monkeypatch.setattr("subprocess.run", fake_gh)
    args = [a for a in setup_args(env_file) if a not in ("--token", TOKEN)]
    result = invoke(*args)
    assert result.exit_code == 0, result.output
    assert "the gh login is not written" in result.output
    assert "[   warn] coordinator.env" in result.output
    assert "OCTOPUS_GITHUB_TOKEN" not in read_env_file(env_file)


def test_coordinator_token_flag(gh, env_file):
    result = invoke(*setup_args(env_file, "--coordinator-token", "github_pat_COORD"))
    assert result.exit_code == 0
    assert read_env_file(env_file)["OCTOPUS_GITHUB_TOKEN"] == "github_pat_COORD"
    assert "github_pat_COORD" not in result.output


def test_failed_ping_exits_1_with_a_hint(gh, env_file):
    gh.ping_status = 0
    result = invoke(*setup_args(env_file, "--ping-timeout", "0"))
    assert result.exit_code == 1
    assert "[   fail] ping" in result.output and "next: start the coordinator" in result.output


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--webhook-url", URL], "missing --repo"),
        (["--repo", REPO], "missing --webhook-url"),
        (["--repo", "acme", "--webhook-url", URL], "bad --repo"),
        (["--repo", REPO, "--webhook-url", "http://octopus.example.org/hook"], "https://"),
    ],
)
def test_usage_errors(gh, env_file, args, message):
    result = invoke("setup", *args, "--token", TOKEN, "--env-file", str(env_file), "--yes")
    assert result.exit_code == 2 and message in result.output
    assert gh.requests == []


def test_no_prompt_without_yes_when_not_interactive(gh, env_file):
    args = [a for a in setup_args(env_file) if a != "--yes"]
    result = invoke(*args)
    assert result.exit_code == 2 and "--yes" in result.output
    assert gh.requests == []


def test_no_token(gh, env_file, monkeypatch):
    def no_gh(cmd, **kw):
        raise FileNotFoundError("gh")

    monkeypatch.setattr("subprocess.run", no_gh)
    result = invoke(
        "setup", "--repo", REPO, "--webhook-url", URL, "--env-file", str(env_file), "--yes"
    )
    assert result.exit_code == 3 and "no GitHub token" in result.output


def test_interactive_prompts_and_confirmation(gh, env_file, monkeypatch):
    monkeypatch.setattr(cli_github, "_interactive", lambda: True)
    result = invoke(
        "setup", "--token", TOKEN, "--env-file", str(env_file), input=f"{REPO}\n{URL}\ny\n"
    )
    assert result.exit_code == 0, result.output
    assert "Set up acme/app?" in result.output and gh.hooks


def test_declining_changes_nothing(gh, env_file, monkeypatch):
    monkeypatch.setattr(cli_github, "_interactive", lambda: True)
    result = invoke(*[a for a in setup_args(env_file) if a != "--yes"], input="n\n")
    assert result.exit_code == 1
    assert gh.requests == [] and not env_file.exists()


def test_network_error_exits_4(gh, env_file, monkeypatch):
    def down(request):
        raise httpx.ConnectError("connection refused")

    from octopus.github.token import TokenClient

    monkeypatch.setattr(
        cli_github,
        "_client",
        lambda token, api_url: TokenClient(
            token, client=httpx.Client(transport=httpx.MockTransport(down))
        ),
    )
    result = invoke(*setup_args(env_file))
    assert result.exit_code == 4 and "could not reach GitHub" in result.output


def test_teardown_uses_the_env_file(gh, env_file):
    assert invoke(*setup_args(env_file)).exit_code == 0
    result = invoke("teardown", "--token", TOKEN, "--env-file", str(env_file), "--yes")
    assert result.exit_code == 0, result.output
    assert "[changed] webhook" in result.output and not gh.hooks
    assert not gh.open_pulls()
    assert f"note: {env_file} was left in place" in result.output


def test_teardown_without_a_url_fails_the_webhook_step(gh, env_file):
    result = invoke(
        "teardown", "--repo", REPO, "--token", TOKEN, "--env-file", str(env_file), "--yes"
    )
    assert result.exit_code == 1 and "pass --webhook-url" in result.output
