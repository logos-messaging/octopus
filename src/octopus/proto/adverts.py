from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

from .messages import TOPIC_PREFIX

COORDINATORS_TOPIC = f"{TOPIC_PREFIX}/coordinators/proto"
ADVERT_VERSION = 1
# Anything larger is not an advert; checked before parsing.
MAX_ADVERT_BYTES = 8 * 1024


def _canonical(model: BaseModel, exclude: set[str], domain: bytes) -> bytes:
    body = json.dumps(
        model.model_dump(mode="json", exclude=exclude), sort_keys=True, separators=(",", ":")
    )
    return domain + body.encode()


class CoordinatorCert(BaseModel):
    """The root key's statement that a coordinator's keys are authorised until `not_after`.

    `coordinator_id` is derived from `enc_pub` (the key runners encrypt job offers to);
    `sign_pub` is the Ed25519 key that signs the coordinator's adverts.
    """

    coordinator_id: str
    enc_pub: str
    sign_pub: str
    not_after: float
    root_sig: str = ""

    def signing_bytes(self) -> bytes:
        return _canonical(self, {"root_sig"}, b"octopus/1/cert\n")


class Advert(BaseModel):
    kind: Literal["coordinator-advert"] = "coordinator-advert"
    version: int = ADVERT_VERSION
    cert: CoordinatorCert
    multiaddrs: list[str] = Field(default_factory=list)
    # What the coordinator can schedule: labels, OS, etc.
    capabilities: list[str] = Field(default_factory=list)
    software: str = ""
    sent_at: float
    expires_at: float
    sig: str = ""

    def signing_bytes(self) -> bytes:
        return _canonical(self, {"sig"}, b"octopus/1/advert\n")
