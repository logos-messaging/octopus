"""The light level with the real `octopus` account: only on a Mac made for it (CI).

This creates a user, a group, a sudo rule and a work root on the machine and removes them at the
end, so it never runs unless OCTOPUS_TEST_LIGHT_ACCOUNT=1 (the `light` job of ci.yml, on a hosted
Mac with passwordless sudo).
"""

import getpass
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from test_ci_light import leg, make_repo

from octopus.ci import light_account
from octopus.ci.exec import GitExecutor
from octopus.ci.light import ACCOUNT, Light

pytestmark = [
    pytest.mark.needs_light_account,
    pytest.mark.skipif(
        sys.platform != "darwin" or os.environ.get("OCTOPUS_TEST_LIGHT_ACCOUNT") != "1",
        reason="creates a real account: set OCTOPUS_TEST_LIGHT_ACCOUNT=1 on a Mac made for it",
    ),
]


@pytest.fixture(scope="module")
def account():
    owner = getpass.getuser()
    fd, tmp = tempfile.mkstemp(prefix="octopus-sudoers-")
    os.close(fd)
    light = light_account.install(owner, sudoers_tmp=tmp)
    Path(tmp).unlink(missing_ok=True)
    yield light
    light_account.run_steps(
        light_account.uninstall_steps(f"/Users/{ACCOUNT}"),
        light_account.execute_step,
        tolerate_failure=True,
    )


def test_the_account_is_usable_and_not_an_administrator(account):
    assert account.supported() is None
    state = light_account.inspect(getpass.getuser())
    assert state.exists and state.sudo_ok and not state.admin


def test_it_cannot_read_the_owners_files(account):
    owner_home = Path.home()
    secret = owner_home / ".ssh" / "octopus-test-secret"
    secret.parent.mkdir(exist_ok=True, mode=0o700)
    secret.write_text("secret")
    secret.chmod(0o600)
    try:
        assert light_account.readable_secrets(str(owner_home)) == []
    finally:
        secret.unlink(missing_ok=True)


def test_a_job_runs_as_the_account_and_leaves_nothing(tmp_path, account):
    owner_home = Path(tempfile.mkdtemp(dir=Path.home(), prefix=".octopus-test-"))
    try:
        (owner_home / "owner-secret").write_text("secret")
        script = (
            "echo user=$(/usr/bin/id -un)\n"
            f"cat {owner_home}/owner-secret 2>/dev/null && echo READ-SECRET || echo secret-denied\n"
            f"touch {owner_home}/planted 2>/dev/null && echo PLANTED || echo plant-denied\n"
            "echo ghtoken=${GH_TOKEN:-none}\n"
            "/bin/launchctl list >/dev/null 2>&1 && echo LAUNCHCTL || echo launchctl-denied\n"
            "(sleep 300 &) ; echo started-background\n"
        )
        src, sha = make_repo(tmp_path, script)
        lines = []
        os.environ["GH_TOKEN"] = "OWNER-TOKEN"
        try:
            ex = GitExecutor(tmp_path / "work", url="file://{repo}", poll_s=0.05, isolation=account)
            result = ex.run(leg(str(src), sha), lines.append, lambda: False)
        finally:
            del os.environ["GH_TOKEN"]
        assert result.exit_code == 0, lines
        assert f"user={ACCOUNT}" in lines
        assert "secret-denied" in lines and "plant-denied" in lines
        assert "launchctl-denied" in lines and "ghtoken=none" in lines
        assert "READ-SECRET" not in lines and "PLANTED" not in lines
        assert not (owner_home / "planted").exists()
        assert list(Path(account.work_root).iterdir()) == []
        left = subprocess.run(
            ["pgrep", "-u", ACCOUNT, "sleep"], check=False, capture_output=True, text=True
        )
        assert left.stdout.strip() == "", "the reaper killed what the job left running"
    finally:
        import shutil

        shutil.rmtree(owner_home, ignore_errors=True)


def test_a_custom_light_level_refuses_without_the_account(tmp_path):
    ghost = Light(account="octopus_missing", work_root=str(tmp_path / "octopus-work"))
    (tmp_path / "octopus-work").mkdir()
    src, sha = make_repo(tmp_path, "echo SHOULD-NOT-RUN\n")
    lines = []
    ex = GitExecutor(tmp_path / "work", url="file://{repo}", poll_s=0.05, isolation=ghost)
    result = ex.run(leg(str(src), sha), lines.append, lambda: False)
    assert result.exit_code == 126 and "SHOULD-NOT-RUN" not in lines
