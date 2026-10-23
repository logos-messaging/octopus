import base64
import json
import struct
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from typer.testing import CliRunner

from octopus import cli_ci
from octopus.ci import wizard
from octopus.ci.gh import Gh
from octopus.ci.home import Home
from octopus.ci.runtime import CiRuntime, RuntimeError_, Settings
from octopus.cli import app
from octopus.crypto import generate_signing_key
from octopus.team import NODE_PROOF_NAMESPACE, TeamError, make_member_list
from octopus.team.keyfile import KeyFileError, load_team_key, save_team_key
from octopus.team.sshsig import build_sshsig
from octopus.team.sync import build_list, scope_logins
from octopus.transport import MemoryBus, MemoryTransport

cli = CliRunner()
NOW = 1_700_000_000.0


class FakeGh:
    """Answers the `gh api` calls the wizard makes."""

    def __init__(
        self, *, admin=True, org=True, members=("alice", "bob", "carol"), team_pub=None,
        logged_in=True,
    ):
        self.logged_in = logged_in
        self.admin = admin
        self.org = org
        self.members = list(members)
        self.team_pub = team_pub
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(argv)
        text = " ".join(argv)

        def ok(out):
            return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

        if not self.logged_in:
            return subprocess.CompletedProcess(argv, 4, stdout="", stderr="gh auth login")
        if ".permissions.admin" in text:
            return ok("true\n" if self.admin else "false\n")
        if "user/memberships/orgs" in text:
            return ok("admin\n" if self.admin else "member\n")
        if "users/acme" in text:
            return ok("Organization\n" if self.org else "User\n")
        if "orgs/acme/members" in text:
            return ok("\n".join(self.members) + "\n")
        if "/collaborators" in text:
            return ok("\n".join(self.members) + "\n")
        if "contents/.octopus/team.pub" in text:
            if self.team_pub is None:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="404")
            return ok(base64.b64encode(self.team_pub.encode()).decode() + "\n")
        if argv[-1] == ".login" or text.endswith("user --jq .login"):
            return ok("alice\n")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="unexpected")


def authorized_line(key: Ed25519PrivateKey) -> str:
    def s(b: bytes) -> bytes:
        return struct.pack(">I", len(b)) + b

    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return "ssh-ed25519 " + base64.b64encode(s(b"ssh-ed25519") + s(raw)).decode() + " me"


@pytest.fixture(autouse=True)
def no_container_runtime(monkeypatch):
    """The wizard offers the sandbox when Podman or Docker is installed: not on test machines."""
    monkeypatch.setattr("octopus.ci.wizard.find_runtime", lambda: None)


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A home directory, an SSH key as ssh-keygen would use, and a fake gh."""
    ssh = Ed25519PrivateKey.generate()
    key_file = tmp_path / "id_ed25519"
    key_file.write_text("private")
    (tmp_path / "id_ed25519.pub").write_text(authorized_line(ssh) + "\n")
    monkeypatch.setattr(
        wizard,
        "ssh_keygen_sign",
        lambda msg, path, ns: build_sshsig(ssh, ns, msg),
    )
    monkeypatch.setenv("OCTOPUS_TEAM_PASSPHRASE", "correct horse")
    monkeypatch.setattr(wizard.platform, "system", lambda: "Linux")
    gh = FakeGh()
    monkeypatch.setattr("octopus.ci.gh._default_run", gh)
    return tmp_path, key_file, gh, ssh


def run_init(tmp_path, key_file, *args, answers="", yes=True):
    return cli.invoke(
        app,
        ["ci", "init", "acme/app", "--ssh-key", str(key_file), "--no-service",
         "--data-dir", str(tmp_path / "data"), *(["--yes"] if yes else []), *args],
        input=answers,
    )


# keyfile and sync


def test_team_key_file_round_trip_and_wrong_passphrase(tmp_path):
    key = generate_signing_key()
    save_team_key(key, tmp_path / "team.key", "secret")
    if sys.platform != "win32":  # Windows has no POSIX modes
        assert (tmp_path / "team.key").stat().st_mode & 0o077 == 0
    assert load_team_key(tmp_path / "team.key", "secret").public_key() is not None
    with pytest.raises(KeyFileError, match="passphrase"):
        load_team_key(tmp_path / "team.key", "wrong")
    with pytest.raises(KeyFileError):
        save_team_key(key, tmp_path / "x", "")


class Source:
    def __init__(self, members):
        self.members = members

    def org_members(self, org):
        return self.members

    def collaborators(self, repo):
        return self.members


def test_scope_logins_and_unknown_scopes():
    assert scope_logins(Source(["a"]), "org:acme") == ["a"]
    assert scope_logins(Source(["b"]), "repo:acme/app") == ["b"]
    with pytest.raises(TeamError, match="unknown scope"):
        scope_logins(Source([]), "team:x")
    with pytest.raises(TeamError, match="could not list"):
        scope_logins(Source(None), "org:acme")


def test_a_list_that_shrinks_sharply_is_refused_without_force():
    key = generate_signing_key()
    previous = make_member_list(key, "org:acme", 5, list("abcdefgh"), NOW)
    with pytest.raises(TeamError, match="shrink"):
        build_list(Source(["a", "b"]), "org:acme", key, NOW + 5, previous=previous)
    forced = build_list(Source(["a", "b"]), "org:acme", key, NOW + 5, previous=previous, force=True)
    assert forced.logins == ["a", "b"]
    small = make_member_list(key, "org:acme", 5, ["a", "b"], NOW)
    assert build_list(Source(["a"]), "org:acme", key, NOW + 5, previous=small).version > 5


# the wizard


def test_the_wizard_makes_an_admin_create_the_team_and_add_the_org(machine):
    tmp_path, key_file, *_ = machine
    result = run_init(tmp_path, key_file, answers="y\ny\n")
    assert result.exit_code == 0, result.output
    assert "ADMIN of acme/app" in result.output
    assert "Create the team key now?" in result.output
    assert 'members of the "acme" organization' in result.output
    home = Home(tmp_path / "data" / "ci")
    assert home.team_key_path.exists() and home.team_pub()
    (ml,) = home.lists()
    assert ml.logins == ["alice", "bob", "carol"] and ml.scope == "org:acme"
    assert home.scopes() == ["org:acme"] and home.delegate() is not None
    assert home.proof().login == "alice"
    assert home.allow().repos == ["acme/app"]


def test_running_the_wizard_again_changes_nothing_and_asks_nothing(machine):
    tmp_path, key_file, *_ = machine
    run_init(tmp_path, key_file, answers="y\ny\n")
    again = run_init(tmp_path, key_file)
    assert again.exit_code == 0, again.output
    assert "synced daily" in again.output and "Create the team key" not in again.output


def test_a_member_pins_the_team_key_from_the_repo(machine):
    tmp_path, key_file, gh, _ = machine
    gh.admin = False
    gh.team_pub = "TEAM-PUBLIC-KEY"
    result = run_init(tmp_path, key_file)
    assert result.exit_code == 0, result.output
    assert "member of acme/app" in result.output and "ADMIN" not in result.output
    home = Home(tmp_path / "data" / "ci")
    assert home.team_pub() == "TEAM-PUBLIC-KEY"
    assert not home.team_key_path.exists() and home.proof() is not None


def test_a_member_without_a_team_in_the_repo_is_told_to_ask_an_admin(machine):
    tmp_path, key_file, gh, _ = machine
    gh.admin = False
    result = run_init(tmp_path, key_file)
    assert result.exit_code == 1
    assert "ask an admin" in result.output


def test_without_a_gh_login_the_user_is_told_to_log_in_not_to_ask_an_admin(machine):
    tmp_path, key_file, gh, _ = machine
    gh.logged_in = False
    result = run_init(tmp_path, key_file)
    assert result.exit_code == 1
    assert "gh auth login" in result.output
    assert "team.pub" not in result.output and "ask an admin" not in result.output
    assert not (tmp_path / "data" / "ci").exists()


def test_no_ssh_key_is_a_clear_failure(machine):
    tmp_path, key_file, gh, _ = machine
    gh.admin, gh.team_pub = False, "K"
    (tmp_path / "id_ed25519.pub").unlink()
    result = run_init(tmp_path, key_file)
    assert result.exit_code == 1 and "ssh-keygen -t ed25519" in result.output


def test_a_user_owned_repo_adds_its_collaborators(machine):
    tmp_path, key_file, gh, _ = machine
    gh.org = False
    result = run_init(tmp_path, key_file, answers="y\ny\n")
    assert result.exit_code == 0, result.output
    assert "collaborators with write access to acme/app" in result.output
    assert Home(tmp_path / "data" / "ci").lists()[0].scope == "repo:acme/app"


def test_the_service_is_installed_unless_declined(machine, monkeypatch):
    tmp_path, key_file, *_ = machine
    installed = []
    monkeypatch.setattr(
        wizard.service,
        "install",
        lambda spec, *a, **k: installed.append(spec) or wizard.service.ServiceResult(tmp_path),
    )
    result = cli.invoke(
        app,
        ["ci", "init", "acme/app", "--ssh-key", str(key_file), "--yes",
         "--data-dir", str(tmp_path / "data")],
        input="y\ny\n",
    )
    assert result.exit_code == 0, result.output
    (spec,) = installed
    assert list(spec.argv[-3:]) == ["octopus", "ci", "node"] and spec.name == "ci"


def test_the_owner_must_agree_that_a_job_can_read_their_login(machine):
    tmp_path, key_file, *_ = machine
    declined = run_init(tmp_path, key_file, answers="y\ny\nn\n", yes=False)
    assert declined.exit_code == 1
    assert "can read your GitHub login" in declined.output and "SSH keys and agent" in declined.output
    home = Home(tmp_path / "data" / "ci")
    assert home.allow().repos == []  # the repo was not allowed
    accepted = run_init(tmp_path, key_file, answers="y\n", yes=False)
    assert accepted.exit_code == 0, accepted.output
    assert home.allow().repos == ["acme/app"]


def test_yes_skips_the_question_but_not_the_notice(machine):
    tmp_path, key_file, *_ = machine
    result = run_init(tmp_path, key_file, answers="y\ny\n")
    assert result.exit_code == 0, result.output
    assert "can read your GitHub login" in result.output and "acknowledged with --yes" in result.output


def test_only_linux_and_macos(machine, monkeypatch):
    tmp_path, key_file, *_ = machine
    monkeypatch.setattr(wizard.platform, "system", lambda: "Windows")
    assert run_init(tmp_path, key_file).exit_code == 1


# the runtime built from that home


def make_runtime(tmp_path, fake_gh=None):
    home = Home(tmp_path / "data" / "ci")
    return CiRuntime(
        home, MemoryTransport(MemoryBus()), Settings("linux", "x64"), gh=Gh(fake_gh or FakeGh())
    )


def test_the_runtime_starts_from_what_the_wizard_saved(machine):
    tmp_path, key_file, *_ = machine
    run_init(tmp_path, key_file, answers="y\ny\n")
    runtime = make_runtime(tmp_path)
    assert runtime.team.is_member_login("alice")
    runtime.start()
    runtime.stop()


def test_the_runtime_needs_a_proof_and_matching_keys(tmp_path):
    with pytest.raises(RuntimeError_, match="octopus ci init"):
        make_runtime(tmp_path)


def test_the_daily_sync_of_an_admin_node_uses_its_delegation(machine, monkeypatch):
    tmp_path, key_file, gh, _ = machine
    run_init(tmp_path, key_file, answers="y\ny\n")
    gh.members = ["alice", "bob", "carol", "dave"]
    runtime = make_runtime(tmp_path, gh)
    assert runtime.sync_now() is True
    assert runtime.team.is_member_login("dave")
    # a failing GitHub keeps the previous list
    gh.members = None
    monkeypatch.setattr(Gh, "org_members", lambda self, org: None)
    assert runtime.sync_now() is False and runtime.team.is_member_login("dave")


def test_a_member_node_does_not_sync(machine):
    tmp_path, key_file, gh, _ = machine
    gh.admin, gh.team_pub = False, "K"
    run_init(tmp_path, key_file)
    # a member has no lists yet and no delegation: nothing to sync
    home = Home(tmp_path / "data" / "ci")
    assert home.delegate() is None


# submit


def test_submit_needs_the_workflow_environment(monkeypatch):
    for name in ("ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    result = cli.invoke(app, ["ci", "submit", "--os", "linux", "--team-key", "K"])
    assert result.exit_code == 2 and "id-token: write" in result.output


def test_submit_falls_back_to_github_when_no_token_can_be_minted(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://example.invalid/?x=1")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/app")

    def boom(*a):
        raise RuntimeError("no network")

    monkeypatch.setattr(cli_ci, "_mint_token", boom)
    result = cli.invoke(
        app, ["ci", "submit", "--os", "linux", "--os", "macos", "--team-key", "K"]
    )
    assert result.exit_code == 0, result.output
    written = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert json.loads(written["handled"]) == []
    assert json.loads(written["unhandled"]) == ["linux", "macos"]
    summary = (tmp_path / "summary.md").read_text()
    assert summary.count("GitHub-hosted runner") == 2
    assert "could not get an OIDC token" in summary


def test_submit_falls_back_and_exits_at_once_when_the_fleet_is_unreachable(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://example.invalid/?x=1")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/app")
    monkeypatch.setattr(cli_ci, "_mint_token", lambda url, tok, aud: "token-for-" + aud)
    exits = []
    monkeypatch.setattr(cli_ci, "_exit_now", lambda: exits.append(True))

    class Down:
        def __init__(self, cfg):
            pass

        def start(self):
            raise OSError("no network")

    monkeypatch.setattr("octopus.transport.logos.LogosTransport", Down)
    result = cli.invoke(app, ["ci", "submit", "--os", "linux", "--team-key", "A" * 43])
    assert result.exit_code == 0, result.output
    written = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert json.loads(written["unhandled"]) == ["linux"]
    assert exits == [True]  # no waiting for the node to stop: the spike measured ~23 s


def test_submit_passes_the_wait_limits_to_the_submitter(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://example.invalid/?x=1")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/app")
    monkeypatch.setattr(cli_ci, "_mint_token", lambda url, tok, aud: "token-for-" + aud)
    monkeypatch.setattr(cli_ci, "_exit_now", lambda: None)
    seen = {}

    class Up:
        def __init__(self, cfg):
            pass

        def start(self):
            pass

    class Quick:
        def __init__(self, transport, team, **kw):
            seen.update(kw)

        def submit(self, legs):
            from octopus.ci.submit import SubmitResult

            return SubmitResult(unhandled=[leg.os for leg in legs])

    monkeypatch.setattr("octopus.transport.logos.LogosTransport", Up)
    monkeypatch.setattr("octopus.ci.submit.Submitter", Quick)
    args = ["ci", "submit", "--os", "macos", "--team-key", "A" * 43]
    assert cli.invoke(app, args).exit_code == 0
    assert seen == {"wait_s": 45.0, "busy_wait_s": 240.0}
    assert cli.invoke(app, [*args, "--wait", "30", "--busy-wait", "120"]).exit_code == 0
    assert seen == {"wait_s": 30.0, "busy_wait_s": 120.0}


def test_the_reusable_workflow_passes_busy_wait_to_submit():
    path = Path(__file__).parent.parent / ".github/workflows/octopus-ci.yml"
    text = path.read_text()
    assert "      busy-wait:\n" in text and "        default: 240\n" in text
    assert "INPUT_BUSY_WAIT: ${{ inputs.busy-wait }}" in text
    assert '--busy-wait "$INPUT_BUSY_WAIT"' in text
    assert "    timeout-minutes: 360\n" in text  # the job stays until the legs end
    assert "--follow" in text and '--max-run "$INPUT_MAX_RUN"' in text
    assert '--silent-timeout "$INPUT_SILENT"' in text
    assert "!cancelled() && needs.submit.outputs.unhandled" in text  # fallback after a failed leg


def test_ci_status_runs_on_an_empty_home(tmp_path):
    result = cli.invoke(app, ["ci", "status", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0 and "octopus ci init" in result.output


def test_the_node_id_in_the_proof_matches_the_runtime_identity(machine):
    tmp_path, key_file, *_ = machine
    run_init(tmp_path, key_file, answers="y\ny\n")
    home = Home(tmp_path / "data" / "ci")
    from octopus.crypto import node_id

    enc, _sign = home.identity()
    assert home.proof().node_id == node_id(enc.public_key())
    assert NODE_PROOF_NAMESPACE.startswith("octopus-node")


# the sandbox offer of the wizard


@pytest.fixture
def linux_with_podman(monkeypatch):
    monkeypatch.setattr("octopus.ci.sandbox.sys.platform", "linux")
    monkeypatch.setattr("octopus.ci.wizard.find_runtime", lambda: "podman")
    pulled = []

    def fake_enable(home, sandbox, *, pull=True, run=None, which=None, say=lambda _t: None):
        pulled.append(sandbox)
        home.save_sandbox(sandbox)
        return [w for w in (sandbox.network_warning(), sandbox.pin_hint()) if w]

    monkeypatch.setattr("octopus.ci.wizard.enable", fake_enable)
    return pulled


def test_the_wizard_offers_the_sandbox_and_enables_it(machine, linux_with_podman):
    tmp_path, key_file, *_ = machine
    digest = "b" * 64
    # team key, members; then: sandbox? image, allow?
    answers = f"y\ny\ny\npython:3.12-slim@sha256:{digest}\ny\n"
    result = run_init(tmp_path, key_file, answers=answers, yes=False)
    assert result.exit_code == 0, result.output
    home = Home(tmp_path / "data" / "ci")
    assert home.sandbox() is not None and home.sandbox().runtime == "podman"
    assert linux_with_podman[0].image == f"python:3.12-slim@sha256:{digest}"
    assert "run in a container" in result.output or "container (python:3.12-slim" in result.output
    assert "can read your GitHub login" not in result.output, "the sandbox notice replaces it"
    assert home.allow().repos == ["acme/app"]


def test_declining_the_sandbox_keeps_the_warning_and_runs_as_the_owner(machine, linux_with_podman):
    tmp_path, key_file, *_ = machine
    result = run_init(tmp_path, key_file, answers="y\ny\nn\ny\n", yes=False)
    assert result.exit_code == 0, result.output
    assert Home(tmp_path / "data" / "ci").sandbox() is None and not linux_with_podman
    assert "can read your GitHub login" in result.output


def test_yes_does_not_enable_the_sandbox_because_there_is_no_image(machine, linux_with_podman):
    tmp_path, key_file, *_ = machine
    result = run_init(tmp_path, key_file, answers="y\ny\n")
    assert result.exit_code == 0, result.output
    assert Home(tmp_path / "data" / "ci").sandbox() is None and not linux_with_podman
    assert "octopus ci sandbox enable" in result.output


def test_a_sandbox_that_cannot_be_enabled_stops_the_wizard(machine, linux_with_podman, monkeypatch):
    from octopus.ci.sandbox_setup import SandboxSetupError

    tmp_path, key_file, *_ = machine

    def refuse(*_a, **_k):
        raise SandboxSetupError("could not pull the image: exit code: 1", 4)

    monkeypatch.setattr("octopus.ci.wizard.enable", refuse)
    result = run_init(tmp_path, key_file, answers="y\ny\ny\npython:3.12-slim\ny\n", yes=False)
    assert result.exit_code == 1 and "could not pull the image" in result.output
    assert Home(tmp_path / "data" / "ci").allow().repos == [], "nothing is allowed unsandboxed"


def test_an_existing_sandbox_is_kept_and_not_asked_again(machine, linux_with_podman):
    from octopus.ci.sandbox import Sandbox

    tmp_path, key_file, *_ = machine
    Home(tmp_path / "data" / "ci").save_sandbox(Sandbox(image="python:3.12-slim", runtime="podman"))
    result = run_init(tmp_path, key_file, answers="y\ny\ny\n", yes=False)
    assert result.exit_code == 0, result.output
    assert not linux_with_podman, "no second prompt"
    assert "jobs run in python:3.12-slim" in result.output


# one delegation per scope


def _delegations(home):
    from octopus.team import Delegation

    key, texts = home.delegate()
    return key, {d.scope: d for d in (Delegation.model_validate_json(t) for t in texts)}


def test_a_sync_renews_a_delegation_for_every_scope_with_one_delegated_key(machine, monkeypatch):
    tmp_path, key_file, *_ = machine
    run_init(tmp_path, key_file, answers="y\ny\n")  # admin: org:acme is synced daily
    monkeypatch.setattr(Gh, "collaborators", lambda self, repo: ["alice", "dave"])
    result = cli.invoke(
        app, ["team", "sync", "--repo", "acme/app", "--data-dir", str(tmp_path / "data")]
    )
    assert result.exit_code == 0, result.output
    home = Home(tmp_path / "data" / "ci")
    assert home.scopes() == ["org:acme", "repo:acme/app"]
    _key, by_scope = _delegations(home)
    assert sorted(by_scope) == ["org:acme", "repo:acme/app"], "the older scope is renewed too"
    assert len({d.delegate_pub for d in by_scope.values()}) == 1, "all for the saved key"


def test_the_daily_sync_renews_every_scope_not_only_the_last_synced(machine, monkeypatch):
    tmp_path, key_file, gh, _ = machine
    run_init(tmp_path, key_file, answers="y\ny\n")
    monkeypatch.setattr(Gh, "collaborators", lambda self, repo: ["alice", "dave"])
    assert cli.invoke(
        app, ["team", "sync", "--repo", "acme/app", "--data-dir", str(tmp_path / "data")]
    ).exit_code == 0
    runtime = make_runtime(tmp_path, gh)
    before = {ml.scope: ml.version for ml in runtime.team.current()}
    assert set(before) == {"org:acme", "repo:acme/app"}
    gh.members = ["alice", "bob", "carol", "erin"]
    monkeypatch.setattr(Gh, "collaborators", lambda self, repo: ["alice", "dave", "frank"])
    assert runtime.sync_now() is True
    after = {ml.scope: ml for ml in runtime.team.current()}
    assert after["org:acme"].version > before["org:acme"] and "erin" in after["org:acme"].logins
    assert after["repo:acme/app"].version > before["repo:acme/app"]
    assert "frank" in after["repo:acme/app"].logins, "the scope that is not the last one works"


def test_a_scope_without_a_delegation_is_skipped_with_a_warning(machine, caplog):
    tmp_path, key_file, gh, _ = machine
    run_init(tmp_path, key_file, answers="y\ny\n")
    home = Home(tmp_path / "data" / "ci")
    home.save_scopes([*home.scopes(), "repo:acme/other"])  # no delegation for it
    runtime = make_runtime(tmp_path, gh)
    gh.members = ["alice", "bob", "carol", "erin"]
    with caplog.at_level("WARNING"):
        assert runtime.sync_now() is True  # org:acme still renewed
    assert "no delegation for repo:acme/other" in caplog.text


def test_a_delegation_file_from_before_one_per_scope_is_still_read(tmp_path):
    home = Home(tmp_path)
    home.save_delegate("KEY", ['{"scope": "org:acme"}'])
    (tmp_path / "delegation.json").write_text('{"scope": "org:acme"}')  # the old single object
    key, texts = home.delegate()
    assert key == "KEY" and len(texts) == 1 and '"org:acme"' in texts[0]
    (tmp_path / "delegation.json").write_text("{nope")
    assert home.delegate() == ("KEY", [])


# logins added by hand stay on the list

SYNC = ["team", "sync"]


def _sync(tmp_path, *args):
    return cli.invoke(app, [*SYNC, *args, "--data-dir", str(tmp_path / "data")])


def test_a_login_added_by_hand_is_kept_by_the_next_sync(machine, monkeypatch):
    tmp_path, key_file, *_ = machine
    run_init(tmp_path, key_file, answers="y\ny\n")
    monkeypatch.setattr(Gh, "collaborators", lambda self, repo: ["alice", "dave"])
    result = _sync(tmp_path, "--repo", "acme/app", "--login", "Octo-Bot")
    assert result.exit_code == 0, result.output
    home = Home(tmp_path / "data" / "ci")
    assert home.extras() == {"repo:acme/app": ["octo-bot"]}
    (ml,) = [m for m in home.lists() if m.scope == "repo:acme/app"]
    assert "octo-bot" in ml.logins
    # a later sync, without --login, as the one the node repeats daily: the bot stays
    monkeypatch.setattr(Gh, "collaborators", lambda self, repo: ["alice", "dave", "erin"])
    assert _sync(tmp_path, "--repo", "acme/app").exit_code == 0
    (ml,) = [m for m in Home(tmp_path / "data" / "ci").lists() if m.scope == "repo:acme/app"]
    assert "octo-bot" in ml.logins and "erin" in ml.logins


def test_the_daily_sync_of_the_node_keeps_the_login_added_by_hand(machine, monkeypatch):
    tmp_path, key_file, gh, _ = machine
    run_init(tmp_path, key_file, answers="y\ny\n")
    monkeypatch.setattr(Gh, "collaborators", lambda self, repo: ["alice", "dave"])
    assert _sync(tmp_path, "--repo", "acme/app", "--login", "octo-bot").exit_code == 0
    runtime = make_runtime(tmp_path, gh)
    monkeypatch.setattr(Gh, "collaborators", lambda self, repo: ["alice", "dave", "frank"])
    assert runtime.sync_now() is True
    (ml,) = [m for m in runtime.team.current() if m.scope == "repo:acme/app"]
    assert "frank" in ml.logins, "the list was renewed"
    assert "octo-bot" in ml.logins, "and the login added by hand was not dropped"


def test_a_login_added_by_hand_only_goes_on_the_scopes_it_was_given_for(machine, monkeypatch):
    tmp_path, key_file, *_ = machine
    run_init(tmp_path, key_file, answers="y\ny\n")  # org:acme
    monkeypatch.setattr(Gh, "collaborators", lambda self, repo: ["alice"])
    assert _sync(tmp_path, "--repo", "acme/app", "--login", "octo-bot").exit_code == 0
    assert _sync(tmp_path, "--org", "acme").exit_code == 0
    by_scope = {m.scope: m.logins for m in Home(tmp_path / "data" / "ci").lists()}
    assert "octo-bot" in by_scope["repo:acme/app"] and "octo-bot" not in by_scope["org:acme"]


def test_forget_login_removes_it_at_that_sync(machine, monkeypatch):
    tmp_path, key_file, *_ = machine
    run_init(tmp_path, key_file, answers="y\ny\n")
    monkeypatch.setattr(Gh, "collaborators", lambda self, repo: ["alice", "dave"])
    assert _sync(tmp_path, "--repo", "acme/app", "--login", "octo-bot").exit_code == 0
    assert _sync(tmp_path, "--repo", "acme/app", "--forget-login", "octo-bot").exit_code == 0
    home = Home(tmp_path / "data" / "ci")
    assert home.extras() == {}
    (ml,) = [m for m in home.lists() if m.scope == "repo:acme/app"]
    assert "octo-bot" not in ml.logins


def test_team_show_says_which_logins_were_added_by_hand(machine, monkeypatch):
    tmp_path, key_file, *_ = machine
    run_init(tmp_path, key_file, answers="y\ny\n")
    monkeypatch.setattr(Gh, "collaborators", lambda self, repo: ["alice"])
    assert _sync(tmp_path, "--repo", "acme/app", "--login", "octo-bot").exit_code == 0
    out = cli.invoke(app, ["team", "show", "--data-dir", str(tmp_path / "data")]).output
    assert "added by hand, kept by every sync: octo-bot" in out


# the name of the machine in the logs


def test_ci_name_sets_shows_and_resets_the_name(tmp_path):
    data = str(tmp_path / "data")
    out = cli.invoke(app, ["ci", "name", "--data-dir", data])
    assert out.exit_code == 0 and out.output.strip()  # the host name
    assert cli.invoke(app, ["ci", "name", "Ivan's Mac", "--data-dir", data]).exit_code == 2
    out = cli.invoke(app, ["ci", "name", "Ivans MacBook (home)", "--data-dir", data])
    assert out.exit_code == 0, out.output
    assert cli.invoke(app, ["ci", "name", "--data-dir", data]).output.strip() == "Ivans MacBook (home)"
    assert "Ivans MacBook (home)" in cli.invoke(app, ["ci", "status", "--data-dir", data]).output
    assert cli.invoke(app, ["ci", "name", "--reset", "--data-dir", data]).exit_code == 0
    assert Home(tmp_path / "data" / "ci").node_name() is None


def test_ci_name_refuses_a_long_or_empty_name(tmp_path):
    data = str(tmp_path / "data")
    assert cli.invoke(app, ["ci", "name", "x" * 41, "--data-dir", data]).exit_code == 2
    assert cli.invoke(app, ["ci", "name", "  ", "--data-dir", data]).exit_code == 2
