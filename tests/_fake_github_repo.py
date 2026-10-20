"""A stateful fake of the GitHub REST endpoints `octopus github setup/teardown` use."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field

import httpx

from octopus.github.token import TokenClient

TOKEN = "github_pat_FAKE0123456789"
REPO = "acme/app"
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
RATE_LIMITED = -403  # fault: a 403 with the primary rate-limit headers


@dataclass
class FakeRepoGitHub:
    repo: str = REPO
    default_branch: str = "main"
    admin: bool | None = True
    scopes: str | None = None  # X-OAuth-Scopes header; None for fine-grained tokens
    exists: bool = True
    # Status the coordinator answers a ping with; None: GitHub never delivers it.
    ping_status: int | None = 200
    hooks: dict[int, dict] = field(default_factory=dict)
    deliveries: dict[int, list[dict]] = field(default_factory=dict)
    refs: dict[str, str] = field(default_factory=lambda: {"main": "sha-main"})
    files: dict[tuple[str, str], str] = field(default_factory=dict)  # (branch, path) -> text
    pulls: list[dict] = field(default_factory=list)
    runners: list[dict] = field(default_factory=list)
    permissions: dict[str, str] = field(default_factory=dict)  # login -> permission
    # (method, path regex) -> statuses (or RATE_LIMITED) to answer before behaving normally
    faults: dict[tuple[str, str], list[int]] = field(default_factory=dict)
    requests: list[tuple[str, str, dict | None]] = field(default_factory=list)
    _next_id: int = 100

    def client(self) -> TokenClient:
        return TokenClient(TOKEN, client=httpx.Client(transport=httpx.MockTransport(self.handle)))

    def add_hook(self, url: str, *, events=("workflow_job",), secret: str | None = "x", **config):
        hook_id = self._id()
        cfg = {"url": url, "content_type": "json", "insecure_ssl": "0", **config}
        if secret is not None:
            cfg["secret"] = secret
        self.hooks[hook_id] = {
            "id": hook_id,
            "name": "web",
            "active": True,
            "events": list(events),
            "config": cfg,
        }
        return hook_id

    def mutations(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p, _ in self.requests if m in MUTATING]

    def open_pulls(self) -> list[dict]:
        return [p for p in self.pulls if p["state"] == "open"]

    # --- routing --------------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        method, path = request.method, request.url.path
        body = json.loads(request.content) if request.content else None
        self.requests.append((method, path, body))
        for (m, pattern), statuses in self.faults.items():
            if m == method and re.fullmatch(pattern, path) and statuses:
                status = statuses.pop(0)
                headers = {}
                if status == RATE_LIMITED:
                    status = 403
                    headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "0"}
                return httpx.Response(status, json={"message": "injected"}, headers=headers)
        base = f"/repos/{self.repo}"
        if not path.startswith(base) or not self.exists:
            return httpx.Response(404, json={"message": "Not Found"})
        rest = path[len(base) :]
        q = request.url.params
        for pattern, fn in self._routes():
            m = re.fullmatch(pattern, f"{method} {rest}")
            if m:
                return fn(body, q, *m.groups())
        return httpx.Response(404, json={"message": f"no fake route: {method} {rest}"})

    def _routes(self):
        return [
            (r"GET ", self._get_repo),
            (r"GET /hooks", self._list_hooks),
            (r"POST /hooks", self._create_hook),
            (r"PATCH /hooks/(\d+)", self._update_hook),
            (r"DELETE /hooks/(\d+)", self._delete_hook),
            (r"POST /hooks/(\d+)/pings", self._ping),
            (r"GET /hooks/(\d+)/deliveries", self._list_deliveries),
            (r"GET /contents/(.+)", self._get_file),
            (r"PUT /contents/(.+)", self._put_file),
            (r"GET /git/ref/heads/(.+)", self._get_ref),
            (r"POST /git/refs", self._create_ref),
            (r"DELETE /git/refs/heads/(.+)", self._delete_ref),
            (r"GET /pulls", self._list_pulls),
            (r"POST /pulls", self._create_pull),
            (r"PATCH /pulls/(\d+)", self._update_pull),
            (r"GET /actions/runners", self._list_runners),
            (r"DELETE /actions/runners/(\d+)", self._delete_runner),
            (r"GET /collaborators/([^/]+)/permission", self._permission),
        ]

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    # --- endpoints ------------------------------------------------------------------------

    def _get_repo(self, body, q):
        data = {"full_name": self.repo, "default_branch": self.default_branch}
        if self.admin is not None:
            data["permissions"] = {"admin": self.admin, "push": True, "pull": True}
        headers = {"X-OAuth-Scopes": self.scopes} if self.scopes is not None else {}
        return httpx.Response(200, json=data, headers=headers)

    def _list_hooks(self, body, q):
        masked = []
        for h in self.hooks.values():
            cfg = dict(h["config"])
            if "secret" in cfg:
                cfg["secret"] = "********"
            masked.append({**h, "config": cfg})
        return httpx.Response(200, json=masked)

    def _create_hook(self, body, q):
        hook_id = self._id()
        self.hooks[hook_id] = {
            "id": hook_id,
            "name": body["name"],
            "active": body["active"],
            "events": body["events"],
            "config": dict(body["config"]),
        }
        return httpx.Response(201, json={"id": hook_id})

    def _update_hook(self, body, q, hook_id):
        hook = self.hooks.get(int(hook_id))
        if hook is None:
            return httpx.Response(404, json={"message": "Not Found"})
        for key in ("events", "active"):
            if key in body:
                hook[key] = body[key]
        if "config" in body:
            hook["config"] = dict(body["config"])  # replaced whole, as on GitHub
        return httpx.Response(200, json={"id": hook["id"]})

    def _delete_hook(self, body, q, hook_id):
        if self.hooks.pop(int(hook_id), None) is None:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(204)

    def _ping(self, body, q, hook_id):
        if self.ping_status is not None:
            status = (
                "OK" if self.ping_status == 200 else f"Invalid HTTP Response: {self.ping_status}"
            )
            self.deliveries.setdefault(int(hook_id), []).insert(
                0,
                {
                    "id": self._id(),
                    "event": "ping",
                    "status_code": self.ping_status,
                    "status": status,
                },
            )
        return httpx.Response(204)

    def _list_deliveries(self, body, q, hook_id):
        return httpx.Response(200, json=self.deliveries.get(int(hook_id), []))

    def _get_file(self, body, q, path):
        text = self.files.get((q.get("ref", self.default_branch), path))
        if text is None:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(
            200, json={"path": path, "content": base64.b64encode(text.encode()).decode()}
        )

    def _put_file(self, body, q, path):
        self.files[(body["branch"], path)] = base64.b64decode(body["content"]).decode()
        return httpx.Response(201, json={"content": {"path": path}})

    def _get_ref(self, body, q, branch):
        if branch not in self.refs:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, json={"object": {"sha": self.refs[branch]}})

    def _create_ref(self, body, q):
        branch = body["ref"].removeprefix("refs/heads/")
        if branch in self.refs:
            return httpx.Response(422, json={"message": "Reference already exists"})
        self.refs[branch] = body["sha"]
        return httpx.Response(201, json={"ref": body["ref"]})

    def _delete_ref(self, body, q, branch):
        if self.refs.pop(branch, None) is None:
            return httpx.Response(422, json={"message": "Reference does not exist"})
        self.files = {k: v for k, v in self.files.items() if k[0] != branch}
        return httpx.Response(204)

    def _list_pulls(self, body, q):
        owner = self.repo.split("/")[0]
        prs = [
            p
            for p in self.pulls
            if p["state"] == q.get("state", "open") and f"{owner}:{p['head']}" == q.get("head")
        ]
        return httpx.Response(200, json=prs)

    def _create_pull(self, body, q):
        number = len(self.pulls) + 1
        pr = {
            "number": number,
            "head": body["head"],
            "base": body["base"],
            "state": "open",
            "title": body["title"],
            "html_url": f"https://github.com/{self.repo}/pull/{number}",
        }
        self.pulls.append(pr)
        return httpx.Response(201, json=pr)

    def _update_pull(self, body, q, number):
        pr = self.pulls[int(number) - 1]
        pr["state"] = body.get("state", pr["state"])
        return httpx.Response(200, json=pr)

    def _list_runners(self, body, q):
        return httpx.Response(200, json={"total_count": len(self.runners), "runners": self.runners})

    def _delete_runner(self, body, q, runner_id):
        self.runners = [r for r in self.runners if r["id"] != int(runner_id)]
        return httpx.Response(204)

    def _permission(self, body, q, login):
        if login not in self.permissions:
            return httpx.Response(404, json={"message": f"{login} is not a user"})
        return httpx.Response(200, json={"permission": self.permissions[login]})
