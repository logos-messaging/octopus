import stat
import sys

import pytest
from typer.testing import CliRunner

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
    return tmp_path / "data"


def runner_tree(data, slug="acme__app"):
    return data / "manual-runner" / slug


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


# register-manual


def test_register_prompts_for_hidden_token_and_runs(fake_runner):
    result = cli.invoke(app, ["runner", "register-manual", "acme/app"], input=TOKEN + "\n")
    assert result.exit_code == 0, result.output
    tree = runner_tree(fake_runner)
    args = (tree / "config.args").read_text().split()
    assert args[:3] == ["--unattended", "--url", "https://github.com/acme/app"]
    assert args[args.index("--labels") + 1] == "octopus"
    assert "--replace" in args
    assert (tree / "run.marker").exists()
    assert "registered:" in result.output


def test_token_goes_through_the_environment_not_argv_nor_output(fake_runner):
    result = cli.invoke(app, ["runner", "register-manual", "acme/app"], input=TOKEN + "\n")
    tree = runner_tree(fake_runner)
    assert (tree / "config.token").read_text() == TOKEN
    assert TOKEN not in (tree / "config.args").read_text()
    assert TOKEN not in result.output


def test_token_from_the_environment(fake_runner, monkeypatch):
    monkeypatch.setenv(manual.TOKEN_ENV, TOKEN)
    result = cli.invoke(app, ["runner", "register-manual", "acme/app", "--no-start"])
    assert result.exit_code == 0, result.output
    assert (runner_tree(fake_runner) / "config.token").read_text() == TOKEN


def test_extra_labels_and_name(fake_runner):
    result = cli.invoke(
        app,
        ["runner", "register-manual", "acme/app", "--labels", "gpu", "--labels", "fast", "--name", "lap"],
        input=TOKEN + "\n",
    )
    assert result.exit_code == 0, result.output
    args = (runner_tree(fake_runner) / "config.args").read_text().split()
    assert args[args.index("--labels") + 1] == "octopus,gpu,fast"
    assert args[args.index("--name") + 1] == "lap"


def test_no_start_registers_only(fake_runner):
    result = cli.invoke(app, ["runner", "register-manual", "acme/app", "--no-start"], input=TOKEN + "\n")
    assert result.exit_code == 0, result.output
    assert not (runner_tree(fake_runner) / "run.marker").exists()


def test_second_run_does_not_ask_for_a_token(fake_runner):
    cli.invoke(app, ["runner", "register-manual", "acme/app", "--no-start"], input=TOKEN + "\n")
    (runner_tree(fake_runner) / "config.args").unlink()
    result = cli.invoke(app, ["runner", "register-manual", "acme/app"])  # no input
    assert result.exit_code == 0, result.output
    assert "already registered" in result.output
    assert not (runner_tree(fake_runner) / "config.args").exists()
    assert (runner_tree(fake_runner) / "run.marker").exists()


def test_reconfigure_asks_again(fake_runner):
    cli.invoke(app, ["runner", "register-manual", "acme/app", "--no-start"], input=TOKEN + "\n")
    result = cli.invoke(
        app, ["runner", "register-manual", "acme/app", "--no-start", "--reconfigure"], input="NEW\n"
    )
    assert result.exit_code == 0, result.output
    assert (runner_tree(fake_runner) / "config.token").read_text() == "NEW"


def test_blank_token_is_an_error(fake_runner):
    result = cli.invoke(app, ["runner", "register-manual", "acme/app"], input="   \n")
    assert result.exit_code == 2
    assert "no registration token given" in result.output


def test_failed_config_reports_and_hides_the_token(fake_runner, monkeypatch):
    monkeypatch.setenv("FAKE_CONFIG_FAIL", "1")
    result = cli.invoke(app, ["runner", "register-manual", "acme/app"], input=TOKEN + "\n")
    assert result.exit_code == 1
    assert "could not configure runner: exit code 1" in result.output
    assert TOKEN not in result.output
    assert "***" in result.output
    assert not (runner_tree(fake_runner) / "run.marker").exists()


def test_runner_exit_code_is_reported(fake_runner, monkeypatch):
    monkeypatch.setenv("FAKE_RUN_EXIT", "3")
    result = cli.invoke(app, ["runner", "register-manual", "acme/app"], input=TOKEN + "\n")
    assert result.exit_code == 1
    assert "runner exited: code: 3" in result.output


def test_bad_target(fake_runner):
    result = cli.invoke(app, ["runner", "register-manual", "a/b/c"])
    assert result.exit_code == 2
    assert "could not read runner target" in result.output


def test_warns_that_octopus_does_not_schedule_it(fake_runner):
    result = cli.invoke(app, ["runner", "register-manual", "acme/app", "--no-start"], input=TOKEN + "\n")
    assert "plain GitHub runner" in result.output


# unregister-manual


def test_unregister_removes_from_github_and_deletes_files(fake_runner):
    cli.invoke(app, ["runner", "register-manual", "acme/app", "--no-start"], input=TOKEN + "\n")
    tree = runner_tree(fake_runner)
    assert tree.is_dir()
    result = cli.invoke(app, ["runner", "unregister-manual", "acme/app"], input="REMOVAL\n")
    assert result.exit_code == 0, result.output
    assert "unregistered from" in result.output
    assert not tree.exists()


def test_unregister_keep_files(fake_runner):
    cli.invoke(app, ["runner", "register-manual", "acme/app", "--no-start"], input=TOKEN + "\n")
    result = cli.invoke(app, ["runner", "unregister-manual", "acme/app", "--keep-files"], input="R\n")
    assert result.exit_code == 0, result.output
    assert runner_tree(fake_runner).is_dir()
    assert (runner_tree(fake_runner) / "config.args").read_text().split() == ["remove"]


def test_unregister_when_nothing_is_registered(fake_runner):
    result = cli.invoke(app, ["runner", "unregister-manual", "acme/app"])
    assert result.exit_code == 0
    assert "nothing registered" in result.output

