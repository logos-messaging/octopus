"""Coordinator discovery: signed adverts authorised by a pinned root key (docs/networking.md)."""

from .authority import AdvertError, issue_cert, make_advert, verify_advert
from .directory import CoordinatorDirectory, LiveCoordinator
from .publisher import ADVERT_INTERVAL_S, AdvertPublisher

__all__ = [
    "ADVERT_INTERVAL_S",
    "AdvertError",
    "AdvertPublisher",
    "CoordinatorDirectory",
    "LiveCoordinator",
    "issue_cert",
    "make_advert",
    "verify_advert",
]
