"""Storage-agnostic domain entities.

These plain dataclasses are the only types that cross the repository boundary,
so nothing outside ``app.db`` depends on a particular database or ORM.
"""
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Role(StrEnum):
    ADMIN = 'admin'
    USER = 'user'


@dataclass(slots=True)
class User:
    id: int
    username: str
    password_hash: str
    role: Role
    created_at: datetime

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN


@dataclass(slots=True)
class Link:
    id: int
    code: str
    target_url: str
    owner_id: int | None  # None for links created anonymously
    created_at: datetime


@dataclass(slots=True)
class GeoLocation:
    country: str | None = None
    country_code: str | None = None
    region: str | None = None
    city: str | None = None
    latitude: float | None = None
    longitude: float | None = None


@dataclass(slots=True)
class IpBlock:
    ip: str  # an IPv4 address, or the /64 network of an IPv6 address
    reason: str
    created_at: datetime
    expires_at: datetime | None  # None blocks until removed by hand


@dataclass(slots=True)
class Click:
    id: int
    link_id: int
    clicked_at: datetime
    ip: str | None = None  # stored truncated, see ``app.services.geoip.anonymize_ip``
    user_agent: str | None = None
    referrer: str | None = None
    country: str | None = None
    country_code: str | None = None
    region: str | None = None
    city: str | None = None
    latitude: float | None = None
    longitude: float | None = None

    @property
    def has_location(self) -> bool:
        return self.latitude is not None and self.longitude is not None
