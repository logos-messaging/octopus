"""A small team on an in-memory bus: members with SSH keys, nodes, a fake GitHub OIDC issuer."""

from __future__ import annotations

import json

from _oidc_keys import FakeIssuer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from octopus.ci.engine import CiConfig, CiNode, ExecResult
from octopus.ci.messages import AUD_PREFIX, leg_audience, make_leg_id
from octopus.crypto import (
    encode_public_key,
    encode_signing_public,
    generate_private_key,
    generate_signing_key,
    node_id,
)
from octopus.oidc import OidcVerifier
from octopus.team import (
    NODE_PROOF_NAMESPACE,
    TeamState,
    make_member_list,
    make_node_proof,
)
from octopus.team.sshsig import build_sshsig
from octopus.transport import MemoryBus, MemoryTransport

PIN = "logos-messaging/octopus/.github/workflows/ci.yml@"
WORKFLOW = PIN + "refs/tags/v1"
REPO = "acme/app"
SHA = "a" * 40
START = 1000.0


class Clock:
    def __init__(self) -> None:
        self.t = START

    def monotonic(self) -> float:
        return self.t

    def now(self) -> float:
        return 1_700_000_000.0 + self.t


class FakeExecutor:
    def __init__(self, exit_code: int = 0, lines=("building", "done")) -> None:
        self.runs: list = []
        self.exit_code = exit_code
        self.lines = lines
        self.stopped_early = False

    def run(self, leg, on_line, should_stop):
        self.runs.append(leg)
        for line in self.lines:
            on_line(line)
        return ExecResult(self.exit_code)


class FakeStatus:
    def __init__(self) -> None:
        self.posts: list = []
        self.descriptions: list[str] = []

    def post(self, leg, state, description):
        self.posts.append((leg.leg_id, state))
        self.descriptions.append(description)


def raw_pub(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


class Machine:
    """One team machine: a node plus the executor and status poster it uses."""

    def __init__(self, world: World, login: str, os_name: str = "linux", hold: bool = False):
        self.world = world
        self.login = login
        self.ssh = Ed25519PrivateKey.generate()
        world.github_keys[login] = [raw_pub(self.ssh)]
        self.enc = generate_private_key()
        self.sign = generate_signing_key()
        self.executor = FakeExecutor()
        self.status = FakeStatus()
        self.transport = MemoryTransport(world.bus)
        self.held: list = []
        proof = make_node_proof(
            login,
            encode_public_key(self.enc.public_key()),
            encode_signing_public(self.sign.public_key()),
            world.clock.now(),
            lambda msg: build_sshsig(self.ssh, NODE_PROOF_NAMESPACE, msg),
        )
        self.team = TeamState(world.team_key.public_key(), world.keys_for, world.clock.now)
        for ml in world.member_lists:
            self.team.accept_list(ml)
        self.node = CiNode(
            transport=self.transport,
            team=self.team,
            proof=proof,
            enc_key=self.enc,
            sign_key=self.sign,
            verifier=OidcVerifier("unused", fetch=world.issuer.jwks, clock=world.clock.monotonic),
            executor=self.executor,
            status=self.status,
            config=CiConfig(
                allowed_repos=frozenset({REPO}), os_name=os_name, workflow_pin=PIN, name=login
            ),
            clock=world.clock,
            spawn=self.held.append if hold else (lambda fn: fn()),
        )
        self.node_id = proof.node_id
        self.frozen = False

    def start(self) -> None:
        self.transport.subscribe("/octopus/1/ci/proto", self.node.handle)

    def tick(self) -> None:
        if not self.frozen:
            self.node.tick()


class World:
    def __init__(self, logins=("alice", "bob", "carol")) -> None:
        self.bus = MemoryBus()
        self.clock = Clock()
        self.issuer = FakeIssuer()
        self.team_key = generate_signing_key()
        self.github_keys: dict[str, list[bytes]] = {}
        self.member_list = make_member_list(
            self.team_key, "org:acme", 1, list(logins), self.clock.now()
        )
        self.member_lists = [self.member_list]  # more lists (other scopes) may be added
        self.machines: list[Machine] = []

    def keys_for(self, login: str) -> list[bytes]:
        return self.github_keys.get(login, [])

    def add(self, login: str, os_name: str = "linux", hold: bool = False) -> Machine:
        m = Machine(self, login, os_name, hold)
        m.start()
        self.machines.append(m)
        return m

    def advance(self, seconds: float, step: float = 0.5) -> None:
        end = self.clock.t + seconds
        while self.clock.t < end:
            self.clock.t += step
            for m in list(self.machines):
                m.tick()

    def request(self, os_name="linux", run_id="77", attempt="1", **claims):
        """What the submit job publishes: a token the OIDC issuer signed for this leg."""
        reply = generate_private_key()
        reply_pub = encode_public_key(reply.public_key())
        defaults = {
            "aud": leg_audience(reply_pub, os_name),
            "repository": REPO,
            "run_id": run_id,
            "run_attempt": attempt,
            "sha": SHA,
            "event_name": "push",
            "job_workflow_ref": WORKFLOW,
        }
        defaults.update(claims)
        token = self.issuer.token(**defaults)
        leg_id = make_leg_id(REPO, run_id, attempt, os_name)
        payload = json.dumps({"kind": "request", "token": token}).encode()
        return leg_id, payload, reply

    def publish(self, payload: bytes) -> None:
        MemoryTransport(self.bus).publish("/octopus/1/ci/proto", payload)

    def settle(self) -> None:
        """Let everyone announce and see each other."""
        self.advance(1.0)


__all__ = ["AUD_PREFIX", "World", "node_id"]
