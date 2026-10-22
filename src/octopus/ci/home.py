"""Where a CI node keeps its identity, team data and settings: `<data dir>/ci/`."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..crypto import load_or_create_private_key, load_or_create_signing_key
from ..team import MemberList, NodeProof

CI_DIR = "ci"


def ci_home(data_dir: Path | None = None) -> Home:
    from ..agent.runner import default_data_dir

    return Home((data_dir or default_data_dir()) / CI_DIR)


@dataclass
class Allow:
    repos: list[str] = field(default_factory=list)
    orgs: list[str] = field(default_factory=list)


class Home:
    def __init__(self, directory: Path) -> None:
        self.dir = directory

    def _path(self, name: str) -> Path:
        return self.dir / name

    @property
    def work_dir(self) -> Path:
        return self._path("work")

    @property
    def node_dir(self) -> Path:
        return self._path("node")

    @property
    def team_key_path(self) -> Path:
        return self._path("team.key")

    def identity(self):
        """(X25519 key for channels, Ed25519 key for signing); created on first use."""
        self.dir.mkdir(parents=True, exist_ok=True)
        return (
            load_or_create_private_key(self._path("node.key")),
            load_or_create_signing_key(self._path("node.sign")),
        )

    def _write(self, name: str, text: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=f".{name}-")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(text)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path(name))
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _read_json(self, name: str):
        try:
            return json.loads(self._path(name).read_text())
        except (OSError, ValueError):
            return None

    # proof

    def save_proof(self, proof: NodeProof) -> None:
        self._write("proof.json", proof.model_dump_json())

    def proof(self) -> NodeProof | None:
        raw = self._read_json("proof.json")
        try:
            return NodeProof.model_validate(raw) if raw is not None else None
        except ValueError:
            return None

    # team

    def save_team_pub(self, pub: str) -> None:
        self._write("team.pub", pub.strip() + "\n")

    def team_pub(self) -> str | None:
        try:
            return self._path("team.pub").read_text().strip() or None
        except OSError:
            return None

    def save_lists(self, lists: list[MemberList]) -> None:
        self._write("members.json", json.dumps([ml.model_dump(mode="json") for ml in lists]))

    def lists(self) -> list[MemberList]:
        raw = self._read_json("members.json") or []
        out = []
        for item in raw if isinstance(raw, list) else []:
            try:
                out.append(MemberList.model_validate(item))
            except ValueError:
                continue
        return out

    def save_delegate(self, delegate_key_pem_b64: str, delegation_json: str) -> None:
        self._write("delegate.key", delegate_key_pem_b64)
        self._write("delegation.json", delegation_json)

    def delegate(self) -> tuple[str, str] | None:
        try:
            return (
                self._path("delegate.key").read_text().strip(),
                self._path("delegation.json").read_text(),
            )
        except OSError:
            return None

    # allowlist and sync scopes

    def save_allow(self, allow: Allow) -> None:
        self._write("allow.json", json.dumps({"repos": allow.repos, "orgs": allow.orgs}))

    def allow(self) -> Allow:
        raw = self._read_json("allow.json") or {}
        return Allow(list(raw.get("repos", [])), list(raw.get("orgs", [])))

    def save_scopes(self, scopes: list[str]) -> None:
        self._write("sync-scopes.json", json.dumps(sorted(set(scopes))))

    def scopes(self) -> list[str]:
        raw = self._read_json("sync-scopes.json")
        return list(raw) if isinstance(raw, list) else []
