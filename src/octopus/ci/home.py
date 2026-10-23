"""Where a CI node keeps its identity, team data and settings: `<data dir>/ci/`."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..crypto import load_or_create_private_key, load_or_create_signing_key
from ..team import MemberList, NodeProof
from .sandbox import Sandbox, SandboxError

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

    def save_delegate(self, delegate_key_pem_b64: str, delegations: list[str]) -> None:
        """The delegated key and one delegation (JSON) per scope: a delegation names one scope."""
        self._write("delegate.key", delegate_key_pem_b64)
        self._write("delegation.json", json.dumps([json.loads(d) for d in delegations]))

    def delegate(self) -> tuple[str, list[str]] | None:
        """(the delegated key, its delegations as JSON), or None when this node never synced.

        A file written before delegations were per scope holds one object, read as a list of one.
        """
        try:
            key = self._path("delegate.key").read_text().strip()
            raw = json.loads(self._path("delegation.json").read_text())
        except OSError:
            return None
        except ValueError:
            return key, []
        items = raw if isinstance(raw, list) else [raw]
        return key, [json.dumps(item) for item in items]

    # allowlist and sync scopes

    def save_allow(self, allow: Allow) -> None:
        self._write("allow.json", json.dumps({"repos": allow.repos, "orgs": allow.orgs}))

    def allow(self) -> Allow:
        raw = self._read_json("allow.json") or {}
        return Allow(list(raw.get("repos", [])), list(raw.get("orgs", [])))

    def save_node_name(self, name: str) -> None:
        self._write("node-name.txt", name.strip() + "\n")

    def node_name(self) -> str | None:
        """The name this machine shows in the logs of the submit job; None: its host name."""
        try:
            return self._path("node-name.txt").read_text().strip() or None
        except OSError:
            return None

    def save_extras(self, extras: dict[str, list[str]]) -> None:
        """Logins added by hand to a scope (`team sync --login`): every sync keeps them."""
        kept = {scope: sorted(set(logins)) for scope, logins in extras.items() if logins}
        self._write("sync-extras.json", json.dumps(kept))

    def extras(self) -> dict[str, list[str]]:
        raw = self._read_json("sync-extras.json")
        if not isinstance(raw, dict):
            return {}
        return {
            scope: sorted({str(lg).strip().lower() for lg in logins if str(lg).strip()})
            for scope, logins in raw.items()
            if isinstance(logins, list)
        }

    def save_scopes(self, scopes: list[str]) -> None:
        self._write("sync-scopes.json", json.dumps(sorted(set(scopes))))

    def scopes(self) -> list[str]:
        raw = self._read_json("sync-scopes.json")
        return list(raw) if isinstance(raw, list) else []

    # sandbox

    def save_sandbox(self, sandbox: Sandbox) -> None:
        self._write("sandbox.json", sandbox.dumps())

    def clear_sandbox(self) -> bool:
        try:
            self._path("sandbox.json").unlink()
        except FileNotFoundError:
            return False
        return True

    def sandbox(self) -> Sandbox | None:
        """The container settings, or None when jobs run as the owner. Raises on a corrupt file."""
        try:
            text = self._path("sandbox.json").read_text()
        except FileNotFoundError:
            return None
        except OSError as e:
            raise SandboxError(f"could not read the sandbox settings: {e}") from e
        return Sandbox.loads(text)
