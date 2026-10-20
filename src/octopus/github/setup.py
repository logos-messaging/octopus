"""Repo-only setup with a token: what `octopus github setup` and `teardown` do.

Configures one repo for Octopus without a GitHub App and without touching the org: a
`workflow_job` webhook with a generated secret, a `coordinator.env` for the coordinator, an
optional PR adding an example workflow, and a `ping` to check the coordinator answers.
Every step is idempotent and reports a `Check`, so a rerun changes nothing.

Secrets (the token, the webhook secret) never appear in checks, logs or exceptions.
"""

from __future__ import annotations

import base64
import os
import secrets
import stat
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import SecretStr

from .app import _error_message, repo_path
from .runners import MAX_DELAY_S, JitRunnerError, JitRunners, _rate_limit_delay
from .token import TokenClient

HOOK_EVENTS = ("workflow_job",)
EXAMPLE_PATH = ".github/workflows/octopus-example.yml"
EXAMPLE_BRANCH = "octopus/example-workflow"
EXAMPLE_TITLE = "ci: add an example Octopus workflow"
EXAMPLE_WORKFLOW = """\
# Runs on a teammate's machine through Octopus (https://github.com/logos-messaging/octopus).
# Only same-repo pushes and PRs from people with write access are served; fork PRs are
# rejected by the coordinator and again by the runner.
name: Octopus example

on:
  push:
    branches: [{default_branch}]
  pull_request:

jobs:
  hello:
    runs-on: [self-hosted, octopus]
    steps:
      - uses: actions/checkout@v7
      - run: echo "Hello from $(uname -sm) via Octopus"
"""
EXAMPLE_PR_BODY = (
    "Adds `" + EXAMPLE_PATH + "`, a minimal workflow that runs on Octopus runners "
    "(`runs-on: [self-hosted, octopus]`).\n\nOpened by `octopus github setup`; "
    "close it if you do not want the example."
)

ENV_REPO = "OCTOPUS_GITHUB_REPO"
ENV_WEBHOOK_URL = "OCTOPUS_WEBHOOK_URL"
ENV_WEBHOOK_SECRET = "OCTOPUS_WEBHOOK_SECRET"
ENV_TOKEN = "OCTOPUS_GITHUB_TOKEN"
ENV_HEADER = (
    "# Octopus coordinator settings, written by `octopus github setup`.",
    "# Holds secrets: keep it private (mode 0600) and out of version control.",
)

PING_POLL_S = 2.0
MAX_ATTEMPTS = 3
BACKOFF_BASE_S = 1.0

HOOK_HINT = "the token needs Webhooks: read and write (fine-grained) or the repo scope"
EXAMPLE_HINT = (
    "the token needs Contents, Pull requests and Workflows: write (fine-grained) or the "
    "repo and workflow scopes; skip with --no-example-pr"
)
RUNNERS_HINT = "the token needs Administration: read and write (fine-grained) or the repo scope"


class Status(str, Enum):
    OK = "ok"  # already as wanted
    CHANGED = "changed"  # done now
    SKIPPED = "skipped"
    WARN = "warn"  # done or not needed, but needs attention
    FAIL = "fail"


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.status is Status.FAIL


@dataclass(frozen=True)
class SetupOptions:
    repo: str
    webhook_url: str
    env_file: Path
    example_pr: bool = True
    ping: bool = True
    ping_timeout_s: float = 30.0
    rotate_secret: bool = False
    # Written as OCTOPUS_GITHUB_TOKEN when set; an existing one in the env file is kept.
    coordinator_token: SecretStr | None = field(default=None, repr=False)


def same_url(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and a.rstrip("/") == b.rstrip("/")


def new_secret() -> str:
    return secrets.token_hex(32)


# --- coordinator.env --------------------------------------------------------------------


def read_env_file(path: Path) -> dict[str, str]:
    """`KEY=value` lines of an env file; comments and blank lines are skipped.

    A missing file reads as empty; other read errors raise `OSError`.
    """
    try:
        text = path.read_text()
    except (FileNotFoundError, NotADirectoryError):
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def write_env_file(path: Path, values: Mapping[str, str]) -> bool:
    """Set `values` in the env file, keeping its other lines. Returns whether it changed.

    Written atomically with mode 0600. Values are unquoted, as systemd's `EnvironmentFile`
    and `docker --env-file` both read them.
    """
    for key, value in values.items():
        if "\n" in value or "\r" in value:
            raise ValueError(f"newline in {key}")
    try:
        old: str | None = path.read_text()
    except (FileNotFoundError, NotADirectoryError):
        old = None
    pending = dict(values)
    lines = []
    for line in old.splitlines() if old is not None else ENV_HEADER:
        key = line.partition("=")[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in pending:
            lines.append(f"{key}={pending.pop(key)}")
        else:
            lines.append(line)
    lines.extend(f"{key}={value}" for key, value in pending.items())
    new = "\n".join(lines) + "\n"
    if new == old and _private(path):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")  # mode 0600
    try:
        with os.fdopen(fd, "w") as f:
            f.write(new)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True


def _private(path: Path) -> bool:
    if os.name == "nt":
        return True
    return stat.S_IMODE(path.stat().st_mode) & 0o077 == 0


# --- GitHub steps -----------------------------------------------------------------------


@dataclass
class HookPlan:
    """What `apply_hook` will do, decided before anything is written."""

    hook_id: int | None  # None: create
    secret: str = field(repr=False)
    patch: dict[str, Any] = field(default_factory=dict, repr=False)
    changes: list[str] = field(default_factory=list)  # human-readable, no secrets


class RepoSetup:
    """The setup and teardown steps for one repo. Network failures raise `httpx.HTTPError`."""

    def __init__(
        self,
        client: TokenClient,
        repo: str,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self.repo = repo
        self._base = repo_path(repo)
        self._owner = repo.partition("/")[0]
        self._sleep = sleep
        self._clock = clock
        self._monotonic = monotonic
        self._info: dict[str, Any] | None = None

    # access

    def check_admin(self) -> Check:
        name = "admin access"
        resp = self._call("GET", self._base)
        if resp.status_code == 401:
            return Check(name, Status.FAIL, "the token was rejected (expired or revoked?)")
        if resp.status_code in (403, 404):
            return Check(name, Status.FAIL, f"{self.repo} not found, or the token cannot see it")
        if resp.status_code != 200:
            return _api_fail(name, "GET", self._base, resp)
        self._info = resp.json()
        scopes = resp.headers.get("X-OAuth-Scopes")
        if scopes is not None:  # classic token or OAuth (gh login); fine-grained has none
            have = {s.strip() for s in scopes.split(",")}
            if not have & {"repo", "admin:repo_hook", "write:repo_hook"}:
                return Check(
                    name,
                    Status.FAIL,
                    "the token lacks the repo scope: run `gh auth refresh -s repo,workflow`",
                )
        admin = (self._info.get("permissions") or {}).get("admin")
        if admin is True:
            return Check(name, Status.OK, self._info.get("full_name", self.repo))
        if admin is False:
            return Check(name, Status.FAIL, f"the token's user is not an admin of {self.repo}")
        return Check(name, Status.WARN, "could not confirm admin access; continuing")

    # webhook

    def plan_hook(
        self, url: str, known_secret: str | None, *, rotate: bool = False
    ) -> tuple[Check | None, HookPlan | None]:
        """Read-only: find our hook and decide what to change. Returns (failure, plan)."""
        path = f"{self._base}/hooks"
        resp = self._call("GET", path, params={"per_page": 100})
        if resp.status_code != 200:
            return _api_fail("webhook", "GET", path, resp, HOOK_HINT), None
        hook = next(
            (h for h in resp.json() if same_url((h.get("config") or {}).get("url"), url)), None
        )
        if hook is None:
            secret = known_secret if known_secret and not rotate else new_secret()
            return None, HookPlan(None, secret, changes=["created"])
        plan = HookPlan(int(hook["id"]), known_secret or "")
        if set(hook.get("events") or ()) != set(HOOK_EVENTS) or not hook.get("active"):
            plan.patch.update(events=list(HOOK_EVENTS), active=True)
            plan.changes.append("events set to workflow_job")
        config = hook.get("config") or {}
        config_ok = (
            config.get("content_type") == "json"
            and str(config.get("insecure_ssl", "0")) == "0"
            and "secret" in config  # GitHub shows a set secret masked
        )
        if rotate or not known_secret or not config_ok:
            if rotate or not known_secret:
                plan.secret = new_secret()
                plan.changes.append(
                    "secret rotated"
                    if rotate
                    else "new secret (the old one is not in the env file)"
                )
            if not config_ok:
                plan.changes.append("config fixed (json, TLS verified, secret)")
            # PATCHing config replaces it whole, so the secret always goes along.
            plan.patch["config"] = {
                "url": url,
                "content_type": "json",
                "insecure_ssl": "0",
                "secret": plan.secret,
            }
        return None, plan

    def apply_hook(self, url: str, plan: HookPlan) -> tuple[Check, int | None]:
        name = "webhook"
        if plan.hook_id is None:
            path = f"{self._base}/hooks"
            body = {
                "name": "web",
                "active": True,
                "events": list(HOOK_EVENTS),
                "config": {
                    "url": url,
                    "content_type": "json",
                    "insecure_ssl": "0",
                    "secret": plan.secret,
                },
            }
            resp = self._call("POST", path, json=body)
            if resp.status_code != 201:
                return _api_fail(name, "POST", path, resp, HOOK_HINT), None
            hook_id = int(resp.json()["id"])
            return Check(
                name, Status.CHANGED, f"created (id {hook_id}), workflow_job only"
            ), hook_id
        if not plan.patch:
            return Check(name, Status.OK, f"exists (id {plan.hook_id})"), plan.hook_id
        path = f"{self._base}/hooks/{plan.hook_id}"
        resp = self._call("PATCH", path, json=plan.patch)
        if resp.status_code != 200:
            return _api_fail(name, "PATCH", path, resp, HOOK_HINT), None
        detail = f"updated (id {plan.hook_id}): {', '.join(plan.changes)}"
        return Check(name, Status.CHANGED, detail), plan.hook_id

    def remove_hook(self, url: str | None) -> Check:
        name = "webhook"
        if not url:
            return Check(name, Status.FAIL, "webhook URL unknown: pass --webhook-url")
        path = f"{self._base}/hooks"
        resp = self._call("GET", path, params={"per_page": 100})
        if resp.status_code != 200:
            return _api_fail(name, "GET", path, resp, HOOK_HINT)
        ids = [
            int(h["id"]) for h in resp.json() if same_url((h.get("config") or {}).get("url"), url)
        ]
        if not ids:
            return Check(name, Status.OK, f"none for {url}")
        for hook_id in ids:
            hook_path = f"{path}/{hook_id}"
            resp = self._call("DELETE", hook_path)
            if resp.status_code not in (204, 404):
                return _api_fail(name, "DELETE", hook_path, resp, HOOK_HINT)
        return Check(name, Status.CHANGED, f"deleted (id {', '.join(map(str, ids))})")

    def ping(self, hook_id: int, timeout_s: float) -> Check:
        """Ask GitHub to ping the hook and wait for the delivery's response code."""
        name = "ping"
        deliveries = f"{self._base}/hooks/{hook_id}/deliveries"
        seen = self._deliveries(deliveries)
        if isinstance(seen, Check):
            return seen
        before = {d.get("id") for d in seen}
        path = f"{self._base}/hooks/{hook_id}/pings"
        resp = self._call("POST", path)
        if resp.status_code != 204:
            return _api_fail(name, "POST", path, resp, HOOK_HINT)
        deadline = self._monotonic() + timeout_s
        while True:
            seen = self._deliveries(deliveries)
            if isinstance(seen, Check):
                return seen
            new = [d for d in seen if d.get("event") == "ping" and d.get("id") not in before]
            if new:
                return _ping_result(max(new, key=lambda d: d.get("id") or 0))
            if self._monotonic() >= deadline:
                return Check(
                    name,
                    Status.FAIL,
                    f"no ping delivery within {timeout_s:.0f}s; check the webhook's Recent "
                    "Deliveries on GitHub",
                )
            self._sleep(PING_POLL_S)

    def _deliveries(self, path: str) -> list[dict[str, Any]] | Check:
        resp = self._call("GET", path, params={"per_page": 30})
        if resp.status_code != 200:
            return _api_fail("ping", "GET", path, resp, HOOK_HINT)
        return list(resp.json())

    # example workflow

    def ensure_example_pr(self) -> Check:
        name = "example workflow PR"
        try:
            default = self._default_branch()
            if self._file_exists(EXAMPLE_PATH, default):
                return Check(name, Status.OK, f"{EXAMPLE_PATH} is already on {default}")
            open_prs = self._open_example_prs()
            if open_prs:
                return Check(name, Status.OK, f"already open: {open_prs[0]['html_url']}")
            branch = f"{self._base}/git/ref/heads/{quote(EXAMPLE_BRANCH, safe='/')}"
            resp = self._call("GET", branch)
            if resp.status_code == 404:
                base_ref = f"{self._base}/git/ref/heads/{quote(default, safe='/')}"
                sha = self._json("GET", base_ref)["object"]["sha"]
                body = {"ref": f"refs/heads/{EXAMPLE_BRANCH}", "sha": sha}
                self._json("POST", f"{self._base}/git/refs", 201, json=body)
            elif resp.status_code != 200:
                raise _StepError("GET", branch, resp)
            if not self._file_exists(EXAMPLE_PATH, EXAMPLE_BRANCH):
                content = EXAMPLE_WORKFLOW.format(default_branch=default)
                body = {
                    "message": EXAMPLE_TITLE,
                    "content": base64.b64encode(content.encode()).decode(),
                    "branch": EXAMPLE_BRANCH,
                }
                self._json("PUT", f"{self._base}/contents/{EXAMPLE_PATH}", 201, json=body)
            body = {"title": EXAMPLE_TITLE, "head": EXAMPLE_BRANCH, "base": default}
            pr = self._json(
                "POST", f"{self._base}/pulls", 201, json={**body, "body": EXAMPLE_PR_BODY}
            )
            return Check(name, Status.CHANGED, f"opened {pr['html_url']}")
        except _StepError as e:
            return _api_fail(name, e.method, e.path, e.resp, EXAMPLE_HINT, status=Status.WARN)

    def remove_example_pr(self) -> Check:
        name = "example workflow PR"
        try:
            done = []
            for pr in self._open_example_prs():
                path = f"{self._base}/pulls/{int(pr['number'])}"
                self._json("PATCH", path, json={"state": "closed"})
                done.append(f"closed #{pr['number']}")
            ref = f"{self._base}/git/refs/heads/{quote(EXAMPLE_BRANCH, safe='/')}"
            resp = self._call("DELETE", ref)
            if resp.status_code == 204:
                done.append(f"deleted branch {EXAMPLE_BRANCH}")
            elif resp.status_code not in (404, 422):  # 422: no such ref
                raise _StepError("DELETE", ref, resp)
            default = self._default_branch()
            note = ""
            if self._file_exists(EXAMPLE_PATH, default):
                note = f"{EXAMPLE_PATH} is on {default} and was left in place"
            if not done:
                return Check(name, Status.OK, note or "nothing to remove")
            return Check(name, Status.CHANGED, "; ".join(done + ([note] if note else [])))
        except _StepError as e:
            return _api_fail(name, e.method, e.path, e.resp, EXAMPLE_HINT, status=Status.WARN)

    def _default_branch(self) -> str:
        if self._info is None:
            self._info = self._json("GET", self._base)
        return str(self._info["default_branch"])

    def _file_exists(self, path: str, ref: str) -> bool:
        url = f"{self._base}/contents/{path}"
        resp = self._call("GET", url, params={"ref": ref})
        if resp.status_code == 200:
            return True
        if resp.status_code == 404:
            return False
        raise _StepError("GET", url, resp)

    def _open_example_prs(self) -> list[dict[str, Any]]:
        params = {"state": "open", "head": f"{self._owner}:{EXAMPLE_BRANCH}"}
        return list(self._json("GET", f"{self._base}/pulls", params=params))

    # runners

    def remove_runners(self) -> Check:
        name = "octopus-* runners"
        jit = JitRunners(self._client, sleep=self._sleep, clock=self._clock)
        try:
            runners = jit.list_octopus_runners(self.repo)
        except JitRunnerError as e:
            return Check(name, Status.FAIL, f"{e}; {RUNNERS_HINT}")
        if not runners:
            return Check(name, Status.OK, "none registered")
        busy = [r.name for r in runners if r.busy]
        removed = [r.name for r in runners if not r.busy and jit.delete(self.repo, r.id)]
        failed = len(runners) - len(busy) - len(removed)
        detail = f"removed {len(removed)}"
        if failed:
            return Check(
                name, Status.FAIL, f"{detail}; {failed} could not be removed; {RUNNERS_HINT}"
            )
        if busy:
            return Check(
                name,
                Status.WARN,
                f"{detail}; left {len(busy)} busy ({', '.join(busy)}): rerun when their jobs end",
            )
        return Check(name, Status.CHANGED, detail)

    # HTTP

    def _call(self, method: str, path: str, **kw: Any) -> httpx.Response:
        """One API call, retried on rate limits (never processed), and on 5xx for reads only:
        a write that failed with a 5xx may still have happened."""
        resp = self._client.request(method, path, **kw)
        for attempt in range(1, MAX_ATTEMPTS):
            delay = _rate_limit_delay(resp, self._clock())
            if delay is None:
                if resp.status_code < 500 or method != "GET":
                    return resp
                delay = BACKOFF_BASE_S * 2 ** (attempt - 1)
            self._sleep(min(delay, MAX_DELAY_S))
            resp = self._client.request(method, path, **kw)
        return resp

    def _json(self, method: str, path: str, expect: int = 200, **kw: Any) -> Any:
        resp = self._call(method, path, **kw)
        if resp.status_code != expect:
            raise _StepError(method, path, resp)
        return resp.json()


class _StepError(Exception):
    def __init__(self, method: str, path: str, resp: httpx.Response) -> None:
        self.method, self.path, self.resp = method, path, resp
        super().__init__(f"{method} {path} -> {resp.status_code}")


def _api_fail(
    name: str,
    method: str,
    path: str,
    resp: httpx.Response,
    hint: str = "",
    *,
    status: Status = Status.FAIL,
) -> Check:
    message = _error_message(resp)
    detail = f"{method} {path} -> {resp.status_code}{': ' + message if message else ''}"
    if hint and resp.status_code in (403, 404):
        detail += f" ({hint})"
    return Check(name, status, detail)


def _ping_result(delivery: Mapping[str, Any]) -> Check:
    code = delivery.get("status_code") or 0
    if code == 200:
        return Check("ping", Status.OK, "the coordinator answered 200")
    if code == 401:
        hint = (
            "the coordinator rejected the signature: is it running with this env file's "
            "OCTOPUS_WEBHOOK_SECRET? (--rotate-secret sets a new one on both sides)"
        )
    elif code == 0:
        hint = "GitHub could not reach the URL: is the coordinator running behind public HTTPS?"
    else:
        hint = "check the coordinator's logs"
    return Check(
        "ping",
        Status.FAIL,
        f"the coordinator answered {code} ({delivery.get('status', '?')}): {hint}",
    )


# --- the commands -----------------------------------------------------------------------


def setup(
    client: TokenClient,
    opts: SetupOptions,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
) -> list[Check]:
    """Configure `opts.repo`. Stops early only when a later step cannot work."""
    rs = RepoSetup(client, opts.repo, sleep=sleep, clock=clock, monotonic=monotonic)
    checks = [rs.check_admin()]
    if checks[-1].failed:
        return checks
    try:
        env = read_env_file(opts.env_file)
    except OSError as e:
        return [*checks, Check(opts.env_file.name, Status.FAIL, f"could not read it: {e.strerror}")]
    known = None
    if env.get(ENV_REPO, "").casefold() == opts.repo.casefold() and same_url(
        env.get(ENV_WEBHOOK_URL), opts.webhook_url
    ):
        known = env.get(ENV_WEBHOOK_SECRET) or None
    failure, plan = rs.plan_hook(opts.webhook_url, known, rotate=opts.rotate_secret)
    if failure is not None or plan is None:
        return [*checks, failure or Check("webhook", Status.FAIL, "no plan")]
    # The env file goes first: if the hook update then fails, a rerun reuses its secret.
    checks.append(_write_env(opts, plan.secret, env))
    if checks[-1].failed:
        return checks
    hook_check, hook_id = rs.apply_hook(opts.webhook_url, plan)
    checks.append(hook_check)
    if hook_check.failed or hook_id is None:
        return checks
    if opts.example_pr:
        checks.append(rs.ensure_example_pr())
    else:
        checks.append(Check("example workflow PR", Status.SKIPPED, "--no-example-pr"))
    if opts.ping:
        checks.append(rs.ping(hook_id, opts.ping_timeout_s))
    else:
        checks.append(Check("ping", Status.SKIPPED, "--no-ping"))
    return checks


def teardown(
    client: TokenClient,
    repo: str,
    webhook_url: str | None,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> list[Check]:
    """Remove what `setup` added to the repo: the webhook, `octopus-*` runners, the example PR."""
    rs = RepoSetup(client, repo, sleep=sleep, clock=clock)
    checks = [rs.check_admin()]
    if checks[-1].failed:
        return checks
    return [*checks, rs.remove_hook(webhook_url), rs.remove_runners(), rs.remove_example_pr()]


def _write_env(opts: SetupOptions, secret: str, existing: Mapping[str, str]) -> Check:
    name = opts.env_file.name
    values = {ENV_REPO: opts.repo, ENV_WEBHOOK_URL: opts.webhook_url, ENV_WEBHOOK_SECRET: secret}
    if opts.coordinator_token is not None:
        values[ENV_TOKEN] = opts.coordinator_token.get_secret_value()
    try:
        changed = write_env_file(opts.env_file, values)
    except (OSError, ValueError) as e:
        reason = e.strerror if isinstance(e, OSError) and e.strerror else str(e)
        return Check(name, Status.FAIL, f"could not write {opts.env_file}: {reason}")
    state = "written" if changed else "up to date"
    if opts.coordinator_token is None and not existing.get(ENV_TOKEN):
        return Check(
            name,
            Status.WARN,
            f"{state}, but without a coordinator token: add {ENV_TOKEN} (a repo-scoped "
            "fine-grained token, see docs/github-setup.md) or rerun with --coordinator-token",
        )
    return Check(name, Status.CHANGED if changed else Status.OK, f"{state}: {opts.env_file}")
