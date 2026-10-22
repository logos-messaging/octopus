import base64
import struct

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from octopus.crypto import encode_public_key, generate_private_key
from octopus.crypto.signing import encode_signing_public, generate_signing_key
from octopus.team import (
    NODE_PROOF_NAMESPACE,
    TeamError,
    TeamState,
    issue_delegation,
    make_member_list,
    make_node_proof,
    verify_member_list,
)
from octopus.team.sshsig import (
    SshSigError,
    build_sshsig,
    parse_authorized_keys,
    verify_sshsig,
)

NOW = 1_000_000.0


def raw_pub(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def authorized_line(key: Ed25519PrivateKey) -> str:
    def s(b: bytes) -> bytes:
        return struct.pack(">I", len(b)) + b

    blob = s(b"ssh-ed25519") + s(raw_pub(key))
    return "ssh-ed25519 " + base64.b64encode(blob).decode() + " alice@laptop"


# ssh signatures


def test_sshsig_round_trip_and_tampering():
    key = Ed25519PrivateKey.generate()
    sig = build_sshsig(key, "ns", b"hello")
    assert verify_sshsig(sig, "ns", b"hello") == raw_pub(key)
    with pytest.raises(SshSigError, match="verify"):
        verify_sshsig(sig, "ns", b"hullo")
    with pytest.raises(SshSigError, match="namespace"):
        verify_sshsig(sig, "other", b"hello")
    with pytest.raises(SshSigError):
        verify_sshsig("garbage", "ns", b"hello")


def test_parse_authorized_keys_skips_other_types():
    key = Ed25519PrivateKey.generate()
    text = "ssh-rsa AAAAB3NzaC1yc2E= x\n" + authorized_line(key) + "\nnot a key\n"
    assert parse_authorized_keys(text) == [raw_pub(key)]


# member lists


@pytest.fixture
def team_key():
    return generate_signing_key()


def test_member_list_signed_by_the_team_key(team_key):
    ml = make_member_list(team_key, "org:acme", 1, ["Alice", "bob"], NOW)
    assert ml.logins == ["alice", "bob"]
    verify_member_list(ml, team_key.public_key(), NOW + 10)


def test_member_list_expired_forged_and_tampered(team_key):
    ml = make_member_list(team_key, "org:acme", 1, ["alice"], NOW, ttl_s=100)
    with pytest.raises(TeamError, match="expired"):
        verify_member_list(ml, team_key.public_key(), NOW + 101)
    other = generate_signing_key()
    with pytest.raises(TeamError, match="not signed by the team"):
        verify_member_list(
            make_member_list(other, "org:acme", 1, ["mallory"], NOW), team_key.public_key(), NOW
        )
    tampered = ml.model_copy(update={"logins": ["alice", "mallory"]})
    with pytest.raises(TeamError, match="bad member list signature"):
        verify_member_list(tampered, team_key.public_key(), NOW)


def test_delegated_signer(team_key):
    delegate = generate_signing_key()
    d = issue_delegation(team_key, delegate.public_key(), "org:acme", NOW + 1000)
    ml = make_member_list(delegate, "org:acme", 2, ["alice"], NOW, delegation=d)
    verify_member_list(ml, team_key.public_key(), NOW)
    # wrong scope, expired delegation, someone else's delegation
    wrong_scope = make_member_list(delegate, "org:other", 2, ["x"], NOW, delegation=d)
    with pytest.raises(TeamError, match="another scope"):
        verify_member_list(wrong_scope, team_key.public_key(), NOW)
    with pytest.raises(TeamError, match="delegation expired"):
        verify_member_list(ml, team_key.public_key(), NOW + 1001 + 3600)
    thief = generate_signing_key()
    stolen = make_member_list(thief, "org:acme", 2, ["thief"], NOW, delegation=d)
    with pytest.raises(TeamError, match="another key"):
        verify_member_list(stolen, team_key.public_key(), NOW)
    forged = d.model_copy(update={"scope": "org:other"})
    bad = make_member_list(delegate, "org:other", 2, ["x"], NOW, delegation=forged)
    with pytest.raises(TeamError, match="bad delegation signature"):
        verify_member_list(bad, team_key.public_key(), NOW)


# node proofs and TeamState


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


class Alice:
    """A team member: an SSH key (as GitHub publishes it) and a node."""

    def __init__(self):
        self.ssh = Ed25519PrivateKey.generate()
        self.enc = generate_private_key()
        self.sign = generate_signing_key()

    def proof(self, now=NOW, login="alice"):
        return make_node_proof(
            login,
            encode_public_key(self.enc.public_key()),
            encode_signing_public(self.sign.public_key()),
            now,
            lambda msg: build_sshsig(self.ssh, NODE_PROOF_NAMESPACE, msg),
        )


@pytest.fixture
def world(team_key):
    clock = Clock()
    alice = Alice()
    keys = {"alice": [raw_pub(alice.ssh)]}
    fetched = []

    def github_keys(login):
        fetched.append(login)
        return keys.get(login, [])

    state = TeamState(team_key.public_key(), github_keys, clock)
    ml = make_member_list(team_key, "org:acme", 1, ["alice"], NOW)
    assert state.accept_list(ml)
    return state, alice, keys, clock, fetched


def test_a_listed_login_with_its_ssh_key_is_a_member(world):
    state, alice, *_ = world
    assert state.check_proof(alice.proof())


def test_not_listed_wrong_key_or_expired_is_refused(world):
    state, alice, keys, clock, _ = world
    assert not state.check_proof(alice.proof(login="mallory"))
    keys["mallory"] = [raw_pub(Ed25519PrivateKey.generate())]
    assert not state.check_proof(alice.proof(login="mallory"))
    keys["alice"] = [raw_pub(Ed25519PrivateKey.generate())]  # not her key on GitHub
    other = Alice()
    assert not state.check_proof(other.proof())
    proof = alice.proof()
    clock.now += 31 * 24 * 3600
    assert not state.check_proof(proof)


def test_tampered_proof_is_refused(world):
    state, alice, *_ = world
    proof = alice.proof()
    swapped = proof.model_copy(update={"sign_pub": encode_signing_public(generate_signing_key().public_key())})
    assert not state.check_proof(swapped)
    wrong_id = proof.model_copy(update={"node_id": "x" * 16})
    assert not state.check_proof(wrong_id)


def test_removed_from_the_list_loses_membership_at_once(world, team_key):
    state, alice, *_ = world
    proof = alice.proof()
    assert state.check_proof(proof)
    assert state.accept_list(make_member_list(team_key, "org:acme", 2, ["bob"], NOW))
    assert not state.check_proof(proof)  # the cached ssh check does not keep her in


def test_old_lists_are_ignored_and_expired_lists_drop_everyone(world, team_key):
    state, alice, _, clock, _ = world
    assert not state.accept_list(make_member_list(team_key, "org:acme", 1, ["bob"], NOW))
    assert state.is_member_login("alice")
    clock.now += 4 * 24 * 3600
    assert not state.is_member_login("alice")
    assert not state.check_proof(alice.proof(now=clock.now))


def test_github_keys_are_cached(world):
    state, alice, _, _, fetched = world
    state.check_proof(alice.proof())
    state.check_proof(alice.proof())
    assert fetched == ["alice"]


def test_several_scopes_add_up(world, team_key):
    state, *_ = world
    assert state.accept_list(make_member_list(team_key, "repo:acme/app", 1, ["carol"], NOW))
    assert state.is_member_login("carol") and state.is_member_login("alice")
