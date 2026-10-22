"""Issuing and checking team data, and the node's view of the team."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from ..crypto import KeyMaterialError, decode_public_key, node_id
from ..crypto.signing import decode_signing_public, encode_signing_public, sign, verify
from .models import NODE_PROOF_NAMESPACE, Delegation, MemberList, NodeProof
from .sshsig import SshSigError, parse_authorized_keys, verify_sshsig

log = logging.getLogger(__name__)

# How long a member list lives: a few daily syncs may fail before nodes stop trusting it.
LIST_TTL_S = 3 * 24 * 3600.0
DELEGATION_TTL_S = 30 * 24 * 3600.0
PROOF_TTL_S = 30 * 24 * 3600.0
KEYS_CACHE_S = 3600.0
KEYS_RETRY_S = 60.0
MAX_CLOCK_SKEW_S = 300.0

GithubKeys = Callable[[str], list[bytes]]


class TeamError(ValueError):
    """Team data was rejected. The message names the reason."""


def team_id(team_pub: Ed25519PublicKey) -> str:
    return encode_signing_public(team_pub)[:16]


def issue_delegation(
    team_key: Ed25519PrivateKey, delegate_pub: Ed25519PublicKey, scope: str, not_after: float
) -> Delegation:
    d = Delegation(
        delegate_pub=encode_signing_public(delegate_pub), scope=scope, not_after=not_after
    )
    return d.model_copy(update={"sig": sign(team_key, d.signing_bytes())})


def make_member_list(
    signer: Ed25519PrivateKey,
    scope: str,
    version: int,
    logins: list[str],
    now: float,
    *,
    ttl_s: float = LIST_TTL_S,
    delegation: Delegation | None = None,
) -> MemberList:
    ml = MemberList(
        scope=scope,
        version=version,
        issued_at=now,
        not_after=now + ttl_s,
        logins=sorted({lg.strip().lower() for lg in logins if lg.strip()}),
        signer_pub=encode_signing_public(signer.public_key()),
        delegation=delegation,
    )
    return ml.model_copy(update={"sig": sign(signer, ml.signing_bytes())})


def verify_member_list(ml: MemberList, team_pub: Ed25519PublicKey, now: float) -> None:
    """Raise TeamError unless the list was signed by the team key or a valid delegate."""
    if ml.not_after <= now:
        raise TeamError("member list expired")
    if ml.issued_at > now + MAX_CLOCK_SKEW_S:
        raise TeamError("member list is from the future")
    try:
        signer = decode_signing_public(ml.signer_pub)
    except KeyMaterialError:
        raise TeamError("bad signer key") from None
    if ml.delegation is None:
        if encode_signing_public(signer) != encode_signing_public(team_pub):
            raise TeamError("list not signed by the team key")
    else:
        d = ml.delegation
        if d.not_after <= now:
            raise TeamError("delegation expired")
        if d.scope != ml.scope:
            raise TeamError("delegation is for another scope")
        if d.delegate_pub != ml.signer_pub:
            raise TeamError("delegation is for another key")
        if not verify(team_pub, d.signing_bytes(), d.sig):
            raise TeamError("bad delegation signature")
    if not verify(signer, ml.signing_bytes(), ml.sig):
        raise TeamError("bad member list signature")


def fetch_github_keys(login: str) -> list[bytes]:
    """The Ed25519 keys GitHub publishes for a user (public, no token).

    A user that does not exist has no keys (404). Any other failure (429, 5xx) raises, so the
    caller keeps the keys it already has instead of taking an outage for "no keys".
    """
    resp = httpx.get(f"https://github.com/{login}.keys", timeout=10, follow_redirects=True)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    return parse_authorized_keys(resp.text)


def scope_covers(scope: str, repo: str) -> bool:
    """Whether a member list of `scope` ("org:<name>" or "repo:<owner/name>") speaks for `repo`.

    A list only counts for the repos inside its scope: a login on the list of one org is not a
    member for the legs of another org, whoever signed the list.
    """
    kind, _, name = scope.partition(":")
    wanted = repo.casefold()
    if kind == "org":
        return wanted.split("/", 1)[0] == name.casefold()
    if kind == "repo":
        return wanted == name.casefold()
    return False


def make_node_proof(
    login: str,
    enc_pub: str,
    sign_pub: str,
    now: float,
    ssh_sign: Callable[[bytes], str],
    *,
    ttl_s: float = PROOF_TTL_S,
) -> NodeProof:
    """Bind a node's keys to a GitHub login; `ssh_sign` signs with that user's SSH key."""
    try:
        nid = node_id(decode_public_key(enc_pub))
    except KeyMaterialError as e:
        raise TeamError(f"bad node key: {e}") from e
    proof = NodeProof(
        login=login.strip().lower(),
        node_id=nid,
        enc_pub=enc_pub,
        sign_pub=sign_pub,
        not_after=now + ttl_s,
    )
    return proof.model_copy(update={"ssh_sig": ssh_sign(proof.signing_bytes())})


class TeamState:
    """What a node knows about the team: the pinned team key and the best valid member list."""

    def __init__(
        self,
        team_pub: Ed25519PublicKey,
        github_keys: GithubKeys = fetch_github_keys,
        clock: Callable[[], float] | None = None,
    ) -> None:
        import time

        self.team_pub = team_pub
        self._github_keys = github_keys
        self._now = clock or time.time
        self._lock = threading.Lock()
        self._lists: dict[str, MemberList] = {}  # scope -> best valid list
        self._keys: dict[str, tuple[float, list[bytes]]] = {}
        self._proofs: dict[str, float] = {}  # ssh_sig -> verified until

    def accept_list(self, ml: MemberList) -> bool:
        """Take `ml` if it is valid and newer than the list held. False when ignored."""
        try:
            verify_member_list(ml, self.team_pub, self._now())
        except TeamError as e:
            log.info("ignored a member list: %s", e)
            return False
        with self._lock:
            held = self._lists.get(ml.scope)
            if held is not None and held.version >= ml.version and held.not_after > self._now():
                return False
            self._lists[ml.scope] = ml
        return True

    def current(self) -> list[MemberList]:
        """The valid lists held, one per scope."""
        now = self._now()
        with self._lock:
            return [ml for ml in self._lists.values() if ml.not_after > now]

    def is_member_login(self, login: str, repo: str | None = None) -> bool:
        """Whether `login` is listed. With `repo`, only the lists whose scope covers it count."""
        who = login.strip().lower()
        return any(
            who in ml.logins and (repo is None or scope_covers(ml.scope, repo))
            for ml in self.current()
        )

    def check_proof(self, proof: NodeProof, repo: str | None = None) -> bool:
        """Whether `proof` shows a node of a listed login (of a list covering `repo`, if given).

        Never raises.
        """
        now = self._now()
        if proof.not_after <= now or not self.is_member_login(proof.login, repo):
            return False
        with self._lock:
            if self._proofs.get(proof.ssh_sig, 0.0) > now:
                return True
        try:
            if node_id(decode_public_key(proof.enc_pub)) != proof.node_id:
                return False
            raw = verify_sshsig(proof.ssh_sig, NODE_PROOF_NAMESPACE, proof.signing_bytes())
        except (SshSigError, KeyMaterialError):
            return False
        if raw not in self._login_keys(proof.login):
            return False
        with self._lock:
            self._proofs[proof.ssh_sig] = min(proof.not_after, now + KEYS_CACHE_S)
            if len(self._proofs) > 10_000:
                self._proofs.clear()
        return True

    def _login_keys(self, login: str) -> list[bytes]:
        now = self._now()
        with self._lock:
            cached = self._keys.get(login)
        if cached is not None and cached[0] > now:
            return cached[1]
        try:
            keys = self._github_keys(login)
        except (httpx.HTTPError, ValueError) as e:
            log.warning("could not fetch the SSH keys of a team member: %s", e)
            # Keep the keys held (even stale ones) and try again soon; never cache "no keys"
            # after an error, or one 429 would lock a member out for an hour.
            if cached is not None:
                with self._lock:
                    self._keys[login] = (now + KEYS_RETRY_S, cached[1])
                return cached[1]
            return []
        with self._lock:
            self._keys[login] = (now + KEYS_CACHE_S, keys)
        return keys
