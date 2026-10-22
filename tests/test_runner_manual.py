import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from octopus import cli_manual
from octopus.agent import manual
from octopus.agent.manual import ManualRunnerError, Target, parse_target
from octopus.cli import app

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="fake runner scripts are POSIX sh")

cli = CliRunner()
TOKEN = "AABBCC-registration-token"

CONFIG_SH = """#!/bin/sh
printf '%s\\n' "$@" > "$(dirname "$0")/config.args"
printf '%s' "$ACTIONS_RUNNER_INPUT_TOKEN" > "$(dirname "$0")/config.token"
case "$1" in
  remove) rm -f "$(dirname "$0")/.runner"; exit 0 ;;
esac
if [ -n "$FAKE_CONFIG_FAIL" ]; then
  echo "Http response code: NotFound from 'POST https://api.github.com' with $ACTIONS_RUNNER_INPUT_TOKEN" >&2
  exit 1
fi
echo '{}' > "$(dirname "$0")/.runner"
"""
RUN_SH = """#!/bin/sh
env > "$(dirname "$0")/run.env"
echo started > "$(dirname "$0")/run.marker"
exit "${FAKE_RUN_EXIT:-0}"
"""


@pytest.fixture
def fake_runner(tmp_path, monkeypatch):
    """A cached runner release that holds fake config.sh and run.sh."""
    cache = tmp_path / "cache"
    (cache / "bin").mkdir(parents=True)
    (cache / "bin" / "Runner.Listener").write_text("x")
    for name, body in (("config.sh", CONFIG_SH), ("run.sh", RUN_SH)):
        path = cache / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(manual, "ensure_installed", lambda *a, **k: cache)
    monkeypatch.setenv("OCTOPUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv(manual.TOKEN_ENV, raising=False)
    # No test may reach GitHub through the developer's gh login.
    monkeypatch.setattr(cli_manual, "gh_token", lambda kind, target: None)
    # Nor the developer's runner settings or a real logos node.
    from octopus.agent import config as agent_config
    from octopus.agent import manual_presence, service
    from octopus.transport import MemoryBus, MemoryTransport

    installed = []
    monkeypatch.setattr(
        service,
        "install",
        lambda spec, *a, **k: installed.append(spec) or service.ServiceResult(Path("svc")),
    )
    monkeypatch.setattr(cli_manual, "installed_services", installed, raising=False)
    bus = MemoryBus()
    monkeypatch.setattr(
        agent_config, "settings_path", lambda *a: tmp_path / "runner.settings.json"
    )
    monkeypatch.setattr(
        manual_presence, "default_transport_factory", lambda d: lambda: MemoryTransport(bus)
    )
    return tmp_path / "data"


def runner_tree(data, slug="acme__app"):
    return data / "manual-runner" / slug


def init_manual(*answers, args=()):
    """`octopus init --role manual`; answers are the lines typed at the prompts."""
    return cli.invoke(app, ["init", "--role", "manual", *args], input="\n".join(answers) + "\n")


# fresh registration: target, labels, name, token, announce?, run now?
def fresh(token=TOKEN, labels="", name="", run="y", target="acme/app", announce="n"):
    return (target, labels, name, token, announce, run)


# parsing


@pytest.mark.parametrize(
    ("text", "url", "slug"),
    [
        ("acme/app", "https://github.com/acme/app", "acme__app"),
        ("acme", "https://github.com/acme", "acme"),
        ("https://github.com/acme/app", "https://github.com/acme/app", "acme__app"),
        ("https://ghe.example.org/acme/app/", "https://ghe.example.org/acme/app", "acme__app"),
    ],
)
def test_parse_target(text, url, slug):
    target = parse_target(text)
    assert (target.url, target.slug) == (url, slug)


@pytest.mark.parametrize("text", ["", "a/b/c", "../x", "acme/ap p", "http://github.com/a/b", "a b"])
def test_parse_target_rejects(text):
    with pytest.raises(ManualRunnerError, match="could not read runner target"):
        parse_target(text)


def test_new_runner_page():
    assert parse_target("acme/app").new_runner_page.endswith("/acme/app/settings/actions/runners/new")
    assert "organizations/acme/settings" in Target("acme", None).new_runner_page


# octopus init --role manual


def test_registers_with_a_hidden_token_and_runs(fake_runner):
    result = init_manual(*fresh())
    assert result.exit_code == 0, result.output
    tree = runner_tree(fake_runner)
    args = (tree / "config.args").read_text().split()
    assert args[:3] == ["--unattended", "--url", "https://github.com/acme/app"]
    assert args[args.index("--labels") + 1] == f"octopus,{manual.unique_label(manual.default_name())}"
    assert "--replace" in args
    assert (tree / "run.marker").exists()
    assert "plain GitHub runner" in result.output


def test_token_goes_through_the_environment_not_argv_nor_output(fake_runner):
    result = init_manual(*fresh())
    tree = runner_tree(fake_runner)
    assert (tree / "config.token").read_text() == TOKEN
    assert TOKEN not in (tree / "config.args").read_text()
    assert TOKEN not in result.output


def test_token_from_the_environment(fake_runner, monkeypatch):
    monkeypatch.setenv(manual.TOKEN_ENV, TOKEN)
    result = init_manual("acme/app", "", "", "n", "n")  # no token line
    assert result.exit_code == 0, result.output
    assert (runner_tree(fake_runner) / "config.token").read_text() == TOKEN


def test_extra_labels_and_name(fake_runner):
    result = init_manual(*fresh(labels="gpu, fast", name="lap"))
    assert result.exit_code == 0, result.output
    args = (runner_tree(fake_runner) / "config.args").read_text().split()
    assert args[args.index("--labels") + 1] == "octopus,octopus-lap,gpu,fast"
    assert args[args.index("--name") + 1] == "lap"
    assert manual.saved_label(runner_tree(fake_runner)) == "octopus-lap"


def test_answering_no_registers_without_starting(fake_runner):
    result = init_manual(*fresh(run="n"))
    assert result.exit_code == 0, result.output
    assert (runner_tree(fake_runner) / ".runner").exists()
    assert not (runner_tree(fake_runner) / "run.marker").exists()
    assert "unregister-manual acme/app" in result.output


def test_second_run_does_not_ask_for_a_token(fake_runner):
    init_manual(*fresh(run="n"))
    (runner_tree(fake_runner) / "config.args").unlink()
    result = init_manual("acme/app", "n", "y")  # target, announce, run now; no token, labels or name
    assert result.exit_code == 0, result.output
    assert "already registered" in result.output
    assert not (runner_tree(fake_runner) / "config.args").exists()
    assert (runner_tree(fake_runner) / "run.marker").exists()


def test_blank_token_is_a_failure(fake_runner):
    result = init_manual("acme/app", "", "", "   ")
    assert result.exit_code == 1
    assert "no registration token given" in result.output


def test_bad_target_is_asked_again(fake_runner):
    result = init_manual("a/b/c", *fresh())
    assert result.exit_code == 0, result.output
    assert "could not read runner target" in result.output
    assert (runner_tree(fake_runner) / ".runner").exists()


def test_empty_target_skips(fake_runner):
    result = init_manual("")
    assert result.exit_code == 0
    assert "nothing to register" in result.output


def test_failed_config_reports_and_hides_the_token(fake_runner, monkeypatch):
    monkeypatch.setenv("FAKE_CONFIG_FAIL", "1")
    result = init_manual("acme/app", "", "", TOKEN)
    assert result.exit_code == 1
    assert "could not configure runner: exit code 1" in result.output
    assert TOKEN not in result.output
    assert "***" in result.output
    assert not (runner_tree(fake_runner) / "run.marker").exists()


def test_runner_exit_code_is_reported(fake_runner, monkeypatch):
    monkeypatch.setenv("FAKE_RUN_EXIT", "3")
    result = init_manual(*fresh())
    assert result.exit_code == 1
    assert "exited with code: 3" in result.output


def test_role_is_asked_and_manual_is_one_answer(fake_runner):
    result = cli.invoke(app, ["init"], input="\n".join(["manual", *fresh(run="n")]) + "\n")
    assert result.exit_code == 0, result.output
    assert (runner_tree(fake_runner) / ".runner").exists()


def test_a_failed_copy_leaves_no_tree_behind(fake_runner, monkeypatch):
    real = manual.make_job_dir

    def half(cache, dest):
        real(cache, dest)
        raise OSError("disk full")

    monkeypatch.setattr(manual, "make_job_dir", half)
    result = init_manual(*fresh())
    assert result.exit_code == 1
    assert "could not copy runner" in result.output
    assert not runner_tree(fake_runner).exists()
    assert not list((fake_runner / "manual-runner").glob(".prepare-*"))
    # the next run starts from scratch and works
    monkeypatch.setattr(manual, "make_job_dir", real)
    result = init_manual(*fresh())
    assert result.exit_code == 0, result.output
    assert (runner_tree(fake_runner) / ".runner").exists()


def test_tokens_are_not_in_the_environment_of_the_runner(fake_runner, monkeypatch):
    # Runner.Worker gives its environment to every job step: a token there is readable by any
    # workflow, and a registration token lets a workflow register another runner.
    monkeypatch.setenv(manual.TOKEN_ENV, TOKEN)
    monkeypatch.setenv("ACTIONS_RUNNER_INPUT_TOKEN", TOKEN)
    result = init_manual("acme/app", "", "", "n", "y")
    assert result.exit_code == 0, result.output
    run_env = (runner_tree(fake_runner) / "run.env").read_text()
    assert TOKEN not in run_env
    assert manual.TOKEN_ENV not in run_env
    assert "ACTIONS_RUNNER_INPUT_TOKEN" not in run_env
    # config.sh does get the token, and only through its own variable
    assert (runner_tree(fake_runner) / "config.token").read_text() == TOKEN


# unregister-manual


def test_unregister_removes_from_github_and_deletes_files(fake_runner):
    init_manual(*fresh(run="n"))
    tree = runner_tree(fake_runner)
    result = cli.invoke(app, ["runner", "unregister-manual", "acme/app"], input="REMOVAL\n")
    assert result.exit_code == 0, result.output
    assert "unregistered from" in result.output
    assert not tree.exists()


def test_removal_token_comes_from_its_own_variable(fake_runner, monkeypatch):
    init_manual(*fresh(run="n"))
    monkeypatch.setenv(manual.REMOVAL_TOKEN_ENV, "REMOVAL-ENV")
    result = cli.invoke(app, ["runner", "unregister-manual", "acme/app"])  # no prompt answer
    assert result.exit_code == 0, result.output
    assert "unregistered from" in result.output


def test_registration_variable_is_not_used_for_removal(fake_runner, monkeypatch):
    # A registration token is rejected by GitHub at removal: ask for the right one.
    init_manual(*fresh(run="n"))
    monkeypatch.setenv(manual.TOKEN_ENV, TOKEN)
    result = cli.invoke(app, ["runner", "unregister-manual", "acme/app"], input="REMOVAL\n")
    assert result.exit_code == 0, result.output
    assert "Paste the removal token" in result.output


def test_unregister_keep_files(fake_runner):
    init_manual(*fresh(run="n"))
    result = cli.invoke(app, ["runner", "unregister-manual", "acme/app", "--keep-files"], input="R\n")
    assert result.exit_code == 0, result.output
    assert runner_tree(fake_runner).is_dir()
    assert (runner_tree(fake_runner) / "config.args").read_text().split() == ["remove"]


def test_unregister_when_nothing_is_registered(fake_runner):
    result = cli.invoke(app, ["runner", "unregister-manual", "acme/app"])
    assert result.exit_code == 0
    assert "nothing registered" in result.output


def test_unregister_bad_target(fake_runner):
    result = cli.invoke(app, ["runner", "unregister-manual", "a/b/c"])
    assert result.exit_code == 2


def test_unique_label():
    assert manual.unique_label("lap") == "octopus-lap"
    assert manual.unique_label("octopus-manual-lap") == "octopus-manual-lap"


ENTRY = "/ip4/127.0.0.1/tcp/60000/p2p/16Uiu2HAmExample"


def _announce_setup(monkeypatch, tmp_path):
    from octopus.agent import config, manual_presence
    from octopus.proto import PRESENCE_TOPIC, Presence
    from octopus.transport import MemoryBus, MemoryTransport

    bus = MemoryBus()
    seen = []
    MemoryTransport(bus).subscribe(
        PRESENCE_TOPIC, lambda _t, p: seen.append(Presence.model_validate_json(p))
    )
    monkeypatch.setattr(config, "settings_path", lambda *a: tmp_path / "runner.settings.json")
    monkeypatch.setattr(
        manual_presence, "default_transport_factory", lambda d: lambda: MemoryTransport(bus)
    )
    return seen


def test_announce_asks_for_the_entry_node_and_publishes_the_label(
    fake_runner, monkeypatch, tmp_path
):
    seen = _announce_setup(monkeypatch, tmp_path)
    # target, labels, name, token, announce?, entry node (a bad one first), run now?
    result = init_manual("acme/app", "", "lap", TOKEN, "y", "<placeholder>", ENTRY, "y")
    assert result.exit_code == 0, result.output
    assert "a multiaddr starts with /" in result.output
    assert "runs-on: [self-hosted, octopus-lap]" in result.output
    assert "announcing label octopus-lap" in result.output
    assert seen[0].labels == ["octopus-lap"] and not seen[0].offline
    assert seen[0].target == "acme/app"  # the coordinator picks it only for jobs of acme/app
    assert seen[-1].offline
    saved = json.loads((tmp_path / "runner.settings.json").read_text())
    assert saved["entry_nodes"] == [ENTRY]


def test_announce_moves_the_node_off_a_busy_port(fake_runner, monkeypatch, tmp_path):
    import socket

    _announce_setup(monkeypatch, tmp_path)
    with socket.socket() as busy:
        busy.bind(("0.0.0.0", 0))
        port = busy.getsockname()[1]
        (tmp_path / "runner.settings.json").write_text(
            json.dumps({"entry_nodes": [ENTRY], "tcp_port": port})
        )
        result = init_manual("acme/app", "", "lap", TOKEN, "y", "n")
        assert f"port {port} is in use" in result.output
        saved = json.loads((tmp_path / "runner.settings.json").read_text())
        assert saved["tcp_port"] != port


def test_declining_announce_leaves_the_settings_alone(fake_runner, monkeypatch, tmp_path):
    _announce_setup(monkeypatch, tmp_path)
    result = init_manual(*fresh(name="lap", announce="n"))
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "runner.settings.json").exists()


def test_announce_with_no_multiaddr_uses_the_shared_fleet(fake_runner, monkeypatch, tmp_path):
    seen = _announce_setup(monkeypatch, tmp_path)
    result = init_manual("acme/app", "", "lap", TOKEN, "y", "", "y")
    assert result.exit_code == 0, result.output
    assert "announcing label octopus-lap" in result.output
    assert seen[0].labels == ["octopus-lap"]
    saved = json.loads((tmp_path / "runner.settings.json").read_text())
    assert saved["entry_nodes"] == []


# one command: octopus init owner/repo


def init_repo(*answers, target="acme/app", args=("--foreground",)):
    return cli.invoke(app, ["init", target, *args], input="\n".join(answers) + "\n")


def test_init_with_a_repo_asks_nothing_when_gh_gives_the_token(fake_runner, monkeypatch):
    calls = []

    def fake_gh(kind, target):
        calls.append((kind, target.name))
        return TOKEN

    monkeypatch.setattr(cli_manual, "gh_token", fake_gh)
    result = init_repo()  # no input at all
    assert result.exit_code == 0, result.output
    assert calls == [("registration", "acme/app")]
    assert "with your gh login" in result.output
    assert TOKEN not in result.output
    tree = runner_tree(fake_runner)
    assert (tree / "config.token").read_text() == TOKEN
    assert (tree / "run.marker").exists()  # it ran


def test_init_with_a_repo_falls_back_to_pasting_the_token(fake_runner):
    result = init_repo(TOKEN)
    assert result.exit_code == 0, result.output
    assert (runner_tree(fake_runner) / "config.token").read_text() == TOKEN


def test_the_environment_token_wins_over_gh(fake_runner, monkeypatch):
    monkeypatch.setenv(manual.TOKEN_ENV, TOKEN)
    monkeypatch.setattr(cli_manual, "gh_token", lambda *a: pytest.fail("gh must not be asked"))
    assert init_repo().exit_code == 0


def test_init_with_a_bad_repo_fails(fake_runner):
    result = init_repo(target="a/b/c")
    assert result.exit_code == 1
    assert "could not read runner target" in result.output


def test_a_repo_argument_does_not_go_with_another_role(fake_runner):
    result = cli.invoke(app, ["init", "acme/app", "--role", "coordinator"])
    assert result.exit_code == 2


def test_gh_token_asks_the_api_for_the_target(monkeypatch):
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="TOK\n", stderr="")

    monkeypatch.setattr(cli_manual.subprocess, "run", fake_run)
    assert cli_manual.gh_token("registration", manual.parse_target("acme/app")) == "TOK"
    assert "repos/acme/app/actions/runners/registration-token" in seen[0]
    assert cli_manual.gh_token("remove", manual.parse_target("acme")) == "TOK"
    assert "orgs/acme/actions/runners/remove-token" in seen[1]


def test_gh_token_is_none_when_gh_is_missing_or_refuses(monkeypatch):
    def missing(cmd, **kw):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(cli_manual.subprocess, "run", missing)
    assert cli_manual.gh_token("registration", manual.parse_target("acme/app")) is None
    monkeypatch.setattr(
        cli_manual.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="HTTP 403"),
    )
    assert cli_manual.gh_token("registration", manual.parse_target("acme/app")) is None


def test_init_with_a_repo_installs_a_service_that_announces_over_logos(fake_runner, monkeypatch):
    monkeypatch.setattr(cli_manual, "gh_token", lambda kind, target: TOKEN)
    result = init_repo(args=())  # no --foreground
    assert result.exit_code == 0, result.output
    (spec,) = cli_manual.installed_services
    assert spec.name == "manual-acme__app"
    assert list(spec.argv[-3:]) == ["runner", "run-manual", "acme/app"]
    assert spec.env["OCTOPUS_DATA_DIR"] == str(fake_runner.resolve())
    assert not (runner_tree(fake_runner) / "run.marker").exists()  # the service runs it


def test_run_manual_announces_the_label_and_runs(fake_runner, monkeypatch):
    from octopus.agent import manual_presence

    monkeypatch.setattr(cli_manual, "gh_token", lambda kind, target: TOKEN)
    init_repo(args=())
    seen = []
    real = manual_presence.announce

    def spy(label, base, *, target=None, fast_stop=False):
        seen.append((label, target))
        assert fast_stop  # the service must not wait for the native node to tear down
        return real(label, base, target=target, fast_stop=fast_stop)

    monkeypatch.setattr(manual_presence, "announce", spy)
    result = cli.invoke(app, ["runner", "run-manual", "acme/app"])
    assert result.exit_code == 0, result.output
    assert seen == [(manual.unique_label(manual.default_name()), "acme/app")]
    assert "announcing label" in result.output
    assert (runner_tree(fake_runner) / "run.marker").exists()


def test_run_manual_needs_a_registered_runner(fake_runner):
    result = cli.invoke(app, ["runner", "run-manual", "acme/other"])
    assert result.exit_code == 1
    assert "octopus init acme/other" in result.output


def test_invite_prints_the_command_for_a_teammate(monkeypatch):
    monkeypatch.setattr(cli_manual, "gh_token", lambda kind, target: TOKEN)
    result = cli.invoke(app, ["invite", "acme/app"])
    assert result.exit_code == 0, result.output
    assert f"{manual.TOKEN_ENV}={TOKEN} octopus init acme/app" in result.output


def test_invite_needs_a_token(monkeypatch):
    monkeypatch.setattr(cli_manual, "gh_token", lambda kind, target: None)
    result = cli.invoke(app, ["invite", "acme/app"])
    assert result.exit_code == 1
    assert "admin rights" in result.output


def test_invite_rejects_a_bad_target():
    assert cli.invoke(app, ["invite", "a/b/c"]).exit_code == 2


def test_the_invited_teammate_registers_with_the_token_alone(fake_runner, monkeypatch):
    monkeypatch.setenv(manual.TOKEN_ENV, TOKEN)
    monkeypatch.setattr(cli_manual, "gh_token", lambda *a: pytest.fail("no gh on their machine"))
    result = init_repo()
    assert result.exit_code == 0, result.output
    assert (runner_tree(fake_runner) / "config.token").read_text() == TOKEN


def test_no_token_fails_at_once_and_downloads_nothing(fake_runner, monkeypatch):
    # No gh login, no OCTOPUS_REGISTRATION_TOKEN, no terminal: nothing to paste (#91).
    monkeypatch.setattr(
        manual, "ensure_installed", lambda *a, **k: pytest.fail("no download without a token")
    )
    result = cli.invoke(app, ["init", "acme/app"], input="")
    assert result.exit_code == 1
    assert "gh auth login" in result.output
    assert "octopus invite acme/app" in result.output
    assert "Aborted" not in result.output
    assert not (fake_runner / "data" / "manual-runner").exists()


def test_the_token_comes_before_the_download(fake_runner, monkeypatch):
    order = []
    real_prepare = manual.prepare
    monkeypatch.setattr(cli_manual, "gh_token", lambda kind, target: order.append("token") or TOKEN)
    monkeypatch.setattr(
        manual, "prepare", lambda *a, **k: order.append("download") or real_prepare(*a, **k)
    )
    result = init_repo()
    assert result.exit_code == 0, result.output
    assert order == ["token", "download"]
