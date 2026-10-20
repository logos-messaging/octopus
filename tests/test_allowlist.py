import json
import os
import stat
import sys

import pytest
from typer.testing import CliRunner

from octopus.agent.trust import Allowlist, InvalidTarget, parse_target
from octopus.cli import app

runner = CliRunner()


def test_round_trip_and_normalization(tmp_path):
    path = tmp_path / "sub" / "runner.json"
    a = Allowlist()
    assert a.add("Acme/App") and not a.add("acme/app")
    assert a.add("Acme-Labs")
    a.save(path)
    b = Allowlist.load(path)
    assert b.repos == {"acme/app"} and b.orgs == {"acme-labs"}
    assert b.remove("ACME/app") and not b.remove("acme/app")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_file_is_private(tmp_path):
    path = tmp_path / "runner.json"
    Allowlist(repos={"acme/app"}).save(path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


@pytest.mark.parametrize("bad", ["", "a/b/c", "../x", "acme/", "/app", "ac me", "acme/..", "-acme"])
def test_invalid_targets_rejected(bad):
    with pytest.raises(InvalidTarget):
        parse_target(bad)


def test_missing_file_is_empty(tmp_path):
    a = Allowlist.load(tmp_path / "nope.json")
    assert not a.repos and not a.orgs


def test_corrupt_file_is_an_error_not_an_open_door(tmp_path):
    path = tmp_path / "runner.json"
    path.write_text("{not json")
    with pytest.raises(ValueError, match="could not read allowlist"):
        Allowlist.load(path)


def test_invalid_entries_are_skipped(tmp_path):
    path = tmp_path / "runner.json"
    path.write_text(json.dumps({"repos": ["acme/app", "../evil"], "orgs": ["ok", 7]}))
    a = Allowlist.load(path)
    assert a.repos == {"acme/app"} and a.orgs == {"ok", "7"}


def test_cli_allow_deny_list(tmp_path):
    cfg = str(tmp_path / "runner.json")
    r = runner.invoke(app, ["runner", "list", "--config", cfg])
    assert r.exit_code == 0 and "rejects every job" in r.output
    assert "allowed" in runner.invoke(app, ["runner", "allow", "acme/app", "--config", cfg]).output
    assert "already" in runner.invoke(app, ["runner", "allow", "ACME/app", "--config", cfg]).output
    runner.invoke(app, ["runner", "allow", "acme-labs", "--config", cfg])
    out = runner.invoke(app, ["runner", "list", "--config", cfg]).output
    assert "repo  acme/app" in out and "org   acme-labs" in out
    assert "denied" in runner.invoke(app, ["runner", "deny", "acme/app", "--config", cfg]).output
    assert "was not allowed" in runner.invoke(
        app, ["runner", "deny", "acme/app", "--config", cfg]
    ).output


def test_cli_rejects_bad_target(tmp_path):
    r = runner.invoke(app, ["runner", "allow", "a/b/c", "--config", str(tmp_path / "r.json")])
    assert r.exit_code == 2
