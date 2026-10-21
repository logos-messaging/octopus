"""JIT runner configs: create ephemeral self-hosted runners and clean up the ones never started.

The encoded JIT config is a bearer credential for registering a runner: it is held in a
`SecretStr`, never logged, and never part of an exception message.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from threading import Lock
from typing import TYPE_CHECKING

import httpx
from pydantic import SecretStr

from .app import GitHubApiError, GitHubClient, _error_message, repo_path

if TYPE_CHECKING:
    from ..coordinator.scheduler import JitProvider

log = logging.getLogger(__name__)

NAME_PREFIX = "octopus-"
BASE_LABELS = ("self-hosted", "octopus")
# "Default" runner group; the only one repository-level runners can belong to.
DEFAULT_RUNNER_GROUP_ID = 1
MAX_ATTEMPTS = 4
BACKOFF_BASE_S = 1.0
MAX_DELAY_S = 60.0
# A freshly issued runner is offline until the job starts; do not sweep it too early.
ORPHAN_GRACE_S = 600.0
PAGE_SIZE = 100


class JitRunnerError(Exception):
    """A runner API call failed. The message never contains the JIT config."""


@dataclass(frozen=True)
class JitRunner:
    name: str
    github_runner_id: int
    repo: str
    jit_config: SecretStr = field(repr=False)


@dataclass(frozen=True)
class RegisteredRunner:
    id: int
    name: str
    status: str  # "online" | "offline"
    busy: bool


def _rate_limit_delay(resp: httpx.Response, now: float) -> float | None:
    """Seconds GitHub asks us to wait, or None when this is not a rate-limit response."""
    retry_after = resp.headers.get("Retry-After")
    if retry_after is not None:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            return None
    if resp.status_code in (403, 429) and resp.headers.get("X-RateLimit-Remaining") == "0":
        try:
            return max(0.0, float(resp.headers["X-RateLimit-Reset"]) - now)
        except (KeyError, ValueError):
            return BACKOFF_BASE_S
    if resp.status_code == 429:
        return BACKOFF_BASE_S
    return None


class JitRunners:
    """Creates and removes ephemeral runners (repository scope), with App or token auth."""

    def __init__(
        self,
        app: GitHubClient,
        *,
        runner_group_id: int = DEFAULT_RUNNER_GROUP_ID,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self._app = app
        self._group = runner_group_id
        self._sleep = sleep
        self._clock = clock
        self._max_attempts = max_attempts
        self._lock = Lock()
        # name -> (repo, github runner id, issued_at): what this process created.
        self._issued: dict[str, tuple[str, int, float]] = {}

    def generate(self, repo: str, name: str, labels: Collection[str] = ()) -> JitRunner:
        """Register runner `name` on `repo` and return its JIT config."""
        path = f"{repo_path(repo)}/actions/runners/generate-jitconfig"
        all_labels = list(dict.fromkeys([*BASE_LABELS, *labels]))
        body = {"name": name, "runner_group_id": self._group, "labels": all_labels}
        resp = self._call("POST", repo, path, json=body)
        if resp.status_code != 201:
            raise JitRunnerError(
                f"could not create JIT config: {GitHubApiError('POST', path, resp.status_code, _error_message(resp))}"
            )
        try:
            data = resp.json()
            runner_id = int(data["runner"]["id"])
            encoded = data["encoded_jit_config"]
        except (KeyError, TypeError, ValueError):
            raise JitRunnerError(
                f"could not create JIT config: malformed response from: POST {path}"
            ) from None
        with self._lock:
            self._issued[name] = (repo, runner_id, self._clock())
        return JitRunner(name, runner_id, repo, SecretStr(encoded))

    def delete(self, repo: str, github_runner_id: int) -> bool:
        """Remove a runner. A runner that is already gone counts as removed."""
        path = f"{repo_path(repo)}/actions/runners/{int(github_runner_id)}"
        resp = self._call("DELETE", repo, path)
        if resp.status_code in (204, 404):
            with self._lock:
                for name, (_, rid, _) in list(self._issued.items()):
                    if rid == github_runner_id:
                        del self._issued[name]
            return True
        log.warning("could not delete runner: %s -> %s", path, resp.status_code)
        return False

    def release(self, name: str) -> bool:
        """Delete a runner this process issued (offer timed out, runner lost, job failed)."""
        with self._lock:
            issued = self._issued.get(name)
        if issued is None:
            return False
        repo, runner_id, _ = issued
        return self.delete(repo, runner_id)

    def list_octopus_runners(self, repo: str) -> list[RegisteredRunner]:
        base = f"{repo_path(repo)}/actions/runners"
        runners: list[RegisteredRunner] = []
        page = 1
        while True:
            resp = self._call("GET", repo, base, params={"per_page": PAGE_SIZE, "page": page})
            if resp.status_code != 200:
                raise JitRunnerError(
                    f"could not list runners: {GitHubApiError('GET', base, resp.status_code, _error_message(resp))}"
                )
            items = resp.json().get("runners", [])
            for r in items:
                if str(r.get("name", "")).startswith(NAME_PREFIX):
                    runners.append(
                        RegisteredRunner(
                            id=int(r["id"]),
                            name=r["name"],
                            status=str(r.get("status", "")),
                            busy=bool(r.get("busy", False)),
                        )
                    )
            if len(items) < PAGE_SIZE:
                return runners
            page += 1

    def sweep_orphans(
        self,
        repo: str,
        *,
        keep: Collection[str] = (),
        grace_s: float = ORPHAN_GRACE_S,
    ) -> list[str]:
        """Delete `octopus-*` runners that are offline, idle, not in `keep` and past the grace.

        A runner that never started a job stays registered until GitHub expires it; sweeping
        removes it earlier. Runners this process issued recently are left alone.
        """
        now = self._clock()
        deleted: list[str] = []
        for r in self.list_octopus_runners(repo):
            if r.status == "online" or r.busy or r.name in keep:
                continue
            with self._lock:
                issued = self._issued.get(r.name)
            if issued is not None and now - issued[2] < grace_s:
                continue
            if self.delete(repo, r.id):
                deleted.append(r.name)
        return deleted

    def _call(self, method: str, repo: str, path: str, **kw) -> httpx.Response:
        """One API call with backoff on rate limits and 5xx; other statuses return as-is."""
        installation_id = self._app.repo_installation_id(repo)
        resp = self._app.request(method, path, installation_id, **kw)
        for attempt in range(1, self._max_attempts):
            delay = _rate_limit_delay(resp, self._clock())
            if delay is None:
                if resp.status_code < 500:
                    return resp
                delay = BACKOFF_BASE_S * 2 ** (attempt - 1)
            self._sleep(min(delay, MAX_DELAY_S))
            resp = self._app.request(method, path, installation_id, **kw)
        return resp


def runner_name(runner_id: str, job_id: str, attempt: int) -> str:
    """Unique per (runner, job, attempt) so a retried job never collides with its old runner."""
    return f"{NAME_PREFIX}{runner_id[:8]}-{job_id[:8]}-{attempt}"


def make_jit_provider(
    jit_runners: JitRunners, on_runner: Callable[[str, str], None] | None = None
) -> JitProvider:
    """The scheduler's `JitProvider` backed by GitHub. Failures become `JitConfigError`.

    `on_runner(name, job_id)` is told which job each runner is made for: GitHub may give the
    runner another job (issue #58).
    """
    from ..coordinator.scheduler import JitConfigError

    def provide(job, runner) -> SecretStr:
        name = runner_name(runner.runner_id, job.job_id, job.attempt)
        try:
            config = jit_runners.generate(job.repo, name, job.labels).jit_config
        except (JitRunnerError, GitHubApiError) as e:
            raise JitConfigError(str(e)) from None
        if on_runner is not None:
            on_runner(name, job.job_id)
        return config

    return provide
