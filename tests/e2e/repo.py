"""The GitHub calls of the e2e test: the trigger branch, workflow runs, jobs and runners."""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from octopus.github.app import GitHubApiError, repo_path
from octopus.github.token import TokenClient

WORKFLOWS_DIR = Path(__file__).parent / "workflows"
# The e2e workflows run on a push to a branch with this prefix only.
BRANCH_PREFIX = "octopus-e2e/"


class E2ERepo:
    def __init__(self, github: TokenClient, repo: str) -> None:
        self._github = github
        self.repo = repo
        self._base = repo_path(repo)

    def default_branch(self) -> str:
        return str(self._github.get_json(self._base)["default_branch"])

    def check_workflows(self) -> None:
        """Make sure that the default branch has the workflows in `WORKFLOWS_DIR`.

        The e2e token cannot write workflow files (that needs the Workflows permission), so
        an admin copies them to the repo one time. See docs/e2e.md.
        """
        branch = self.default_branch()
        for local in sorted(WORKFLOWS_DIR.glob("*.yml")):
            path = f".github/workflows/{local.name}"
            try:
                data = self._github.get_json(f"{self._base}/contents/{path}?ref={branch}")
                remote = base64.b64decode(data["content"]).decode()
            except GitHubApiError as e:
                if e.status != 404:
                    raise
                remote = None
            if remote != local.read_text():
                raise AssertionError(
                    f"{self.repo}: {path} on {branch} is missing or differs from "
                    f"tests/e2e/workflows/{local.name}: copy it there (see docs/e2e.md)"
                )

    def push_branch(self, name: str) -> str:
        """Make a branch with a new commit on the default branch. Returns the commit SHA.

        The commit has the same tree as its parent. A new branch is a push event, so the
        e2e workflows start, and the token needs only the Contents permission.
        """
        head = self._github.get_json(f"{self._base}/git/ref/heads/{self.default_branch()}")
        parent = head["object"]["sha"]
        tree = self._github.get_json(f"{self._base}/git/commits/{parent}")["tree"]["sha"]
        commit = self._post(
            "/git/commits",
            {"message": f"octopus e2e: {name}", "tree": tree, "parents": [parent]},
        )["sha"]
        self._post("/git/refs", {"ref": f"refs/heads/{name}", "sha": commit})
        return str(commit)

    def delete_branch(self, name: str) -> None:
        self._github.request("DELETE", f"{self._base}/git/refs/heads/{name}")

    def runs(self, sha: str) -> list[dict[str, Any]]:
        data = self._github.get_json(f"{self._base}/actions/runs?head_sha={sha}&per_page=20")
        return list(data["workflow_runs"])

    def jobs(self, run_id: int) -> list[dict[str, Any]]:
        return list(self._github.get_json(f"{self._base}/actions/runs/{run_id}/jobs")["jobs"])

    def cancel_runs(self, sha: str) -> None:
        for run in self.runs(sha):
            if run["status"] != "completed":
                self._github.request("POST", f"{self._base}/actions/runs/{run['id']}/cancel")

    def wait_jobs(
        self,
        sha: str,
        names: set[str],
        timeout_s: float,
        poll_s: float = 5.0,
        check: Callable[[], None] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Wait until GitHub completes the jobs called `names` of the runs for `sha`.

        `check` runs at each poll. It can raise to stop the wait early, for example when
        Octopus gave up a job that GitHub keeps in the queue.
        """
        deadline = time.monotonic() + timeout_s
        found: dict[str, dict[str, Any]] = {}
        while True:
            if check is not None:
                check()
            found = {}
            for run in self.runs(sha):
                for job in self.jobs(run["id"]):
                    if job["name"] in names:
                        found[job["name"]] = job
            done = {n: j for n, j in found.items() if j["status"] == "completed"}
            if set(done) == names:
                return done
            if time.monotonic() > deadline:
                state = {n: j["status"] for n, j in found.items()}
                raise AssertionError(f"jobs not completed in {timeout_s:.0f}s: {state}")
            time.sleep(poll_s)

    def runner_names(self) -> set[str]:
        data = self._github.get_json(f"{self._base}/actions/runners?per_page=100")
        return {r["name"] for r in data["runners"]}

    def hook_id(self, url: str) -> int:
        for hook in self._github.get_json(f"{self._base}/hooks"):
            if hook.get("config", {}).get("url") == url:
                return int(hook["id"])
        raise AssertionError(f"no webhook for: {url}")

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        resp = self._github.request("POST", f"{self._base}{path}", json=body)
        if resp.status_code != 201:
            raise GitHubApiError("POST", f"{self._base}{path}", resp.status_code, resp.text[:200])
        return resp.json()
