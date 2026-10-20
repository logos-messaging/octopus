import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "github"
SECRET = b"It's a Secret to Everybody"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def octopus_job(name: str = "workflow_job.queued.json", **job_fields) -> dict:
    """A recorded workflow_job payload, retargeted to `runs-on: [self-hosted, octopus]`."""
    payload = load_fixture(name)
    payload["workflow_job"]["labels"] = ["self-hosted", "octopus"]
    payload["workflow_job"].update(job_fields)
    return payload


@pytest.fixture(scope="session", autouse=True)
def _logos_shutdown():
    """Stop the nodes liblogosdelivery still holds before the interpreter exits.

    Without it the process segfaults at exit after the needs_lib tests pass.
    """
    yield
    import sys

    ld = sys.modules.get("logosdelivery")
    if ld is not None:
        ld.shutdown()


@pytest.fixture(scope="session")
def rsa_key_pem() -> bytes:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
