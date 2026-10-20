"""Key management for coordinator discovery: `octopus root ...` and `octopus coordinator keygen`."""

from __future__ import annotations

import json
import time
from pathlib import Path

import typer

from .agent.runner import default_data_dir
from .crypto import (
    KeyMaterialError,
    decode_public_key,
    decode_signing_public,
    encode_public_key,
    encode_signing_public,
    load_or_create_private_key,
    load_or_create_signing_key,
    node_id,
)
from .crypto.signing import load_signing_key
from .discovery import issue_cert

root_app = typer.Typer(
    no_args_is_help=True, help="Root key of the network: authorises coordinators (run offline)."
)

ENC_KEY_FILE = "coordinator.key"
SIGN_KEY_FILE = "coordinator.sign.key"
CERT_FILE = "coordinator.cert.json"
DEFAULT_ROOT_KEY_FILE = Path("octopus-root.key")
DAY_S = 86400.0

RootKeyOutOption = typer.Option(
    DEFAULT_ROOT_KEY_FILE, "--out", help="Where to write the secret key."
)
EncPubOption = typer.Option(..., "--enc-pub", help="Coordinator encryption public key.")
SignPubOption = typer.Option(..., "--sign-pub", help="Coordinator signing public key.")
RootKeyFileOption = typer.Option(DEFAULT_ROOT_KEY_FILE, "--root-key-file")
DaysOption = typer.Option(90.0, "--days", min=0.01, help="How long the authorisation lasts.")
CertOutOption = typer.Option(None, "--out", help="Write the certificate here.")
DataDirOption = typer.Option(None, "--data-dir", help="Where the keys live.")


def _fail(message: str) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code=3)


@root_app.command("keygen")
def root_keygen(
    out: Path = RootKeyOutOption,
) -> None:
    """Create the root key. Keep the file secret and offline; publish only the printed key."""
    if out.exists():
        raise _fail(f"could not create root key: file exists: {out}")
    key = load_or_create_signing_key(out)
    typer.echo(f"root key written: {out} (keep it secret and offline)")
    typer.echo(f"root public key: {encode_signing_public(key.public_key())}")
    typer.echo("runners pin it with: octopus runner setup --root-key <key>")


@root_app.command("authorize")
def root_authorize(
    enc_pub: str = EncPubOption,
    sign_pub: str = SignPubOption,
    root_key_file: Path = RootKeyFileOption,
    days: float = DaysOption,
    out: Path | None = CertOutOption,
) -> None:
    """Sign a certificate authorising a coordinator; give the JSON back to its operator."""
    try:
        root_key = load_signing_key(root_key_file)
        cert = issue_cert(
            root_key,
            decode_public_key(enc_pub),
            decode_signing_public(sign_pub),
            not_after=time.time() + days * DAY_S,
        )
    except (OSError, KeyMaterialError) as e:
        raise _fail(f"could not authorise coordinator: {e}") from e
    text = json.dumps(cert.model_dump(mode="json"), indent=2)
    if out is not None:
        out.write_text(text + "\n")
        typer.echo(f"certificate written: {out} for coordinator: {cert.coordinator_id}")
    else:
        typer.echo(text)


def add_coordinator_keygen(coordinator_app: typer.Typer) -> None:
    @coordinator_app.command("keygen")
    def coordinator_keygen(
        data_dir: Path | None = DataDirOption,
    ) -> None:
        """Create (or show) this coordinator's keys; send the printed keys to the root key holder."""
        base = data_dir or default_data_dir()
        enc = load_or_create_private_key(base / ENC_KEY_FILE)
        sign = load_or_create_signing_key(base / SIGN_KEY_FILE)
        typer.echo(f"coordinator id:  {node_id(enc.public_key())}")
        typer.echo(f"enc public key:  {encode_public_key(enc.public_key())}")
        typer.echo(f"sign public key: {encode_signing_public(sign.public_key())}")
        typer.echo(f"keys stored in:  {base} (certificate expected at {base / CERT_FILE})")
