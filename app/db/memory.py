"""In-memory backend (``DATABASE_URL=memory://``). Data is lost on restart.

Useful for tests and throwaway demos, and as the smallest complete example of
what a backend has to implement.
"""
from dataclasses import replace
from datetime import UTC, datetime
from itertools import count

from .base import BlockRepository, ClickRepository, Database, DuplicateError, LinkRepository, UserRepository
from .models import Click, GeoLocation, IpBlock, Link, Role, User


def _now() -> datetime:
    return datetime.now(UTC)


class MemoryUserRepository(UserRepository):
    def __init__(self, db: 'MemoryDatabase') -> None:
        self._db = db
        self._rows: dict[int, User] = {}
        self._ids = count(1)

    async def add(self, username: str, password_hash: str, role: Role = Role.USER) -> User:
        if any(u.username == username for u in self._rows.values()):
            raise DuplicateError(f'username {username!r} already exists')
        user = User(next(self._ids), username, password_hash, Role(role), _now())
        self._rows[user.id] = user
        return replace(user)

    async def get(self, user_id: int) -> User | None:
        user = self._rows.get(user_id)
        return replace(user) if user else None

    async def get_by_username(self, username: str) -> User | None:
        return next((replace(u) for u in self._rows.values() if u.username == username), None)

    async def list(self) -> list[User]:
        return [replace(u) for u in sorted(self._rows.values(), key=lambda u: u.id)]

    async def set_password(self, user_id: int, password_hash: str) -> None:
        if user_id in self._rows:
            self._rows[user_id].password_hash = password_hash

    async def delete(self, user_id: int) -> None:
        self._rows.pop(user_id, None)
        for link in self._db.links._rows.values():
            if link.owner_id == user_id:
                link.owner_id = None


class MemoryLinkRepository(LinkRepository):
    def __init__(self, db: 'MemoryDatabase') -> None:
        self._db = db
        self._rows: dict[int, Link] = {}
        self._ids = count(1)

    async def add(self, code: str, target_url: str, owner_id: int | None = None) -> Link:
        if any(link.code == code for link in self._rows.values()):
            raise DuplicateError(f'code {code!r} already exists')
        link = Link(next(self._ids), code, target_url, owner_id, _now())
        self._rows[link.id] = link
        return replace(link)

    async def get_by_code(self, code: str) -> Link | None:
        return next((replace(link) for link in self._rows.values() if link.code == code), None)

    async def find_by_target(self, target_url: str, owner_id: int | None) -> Link | None:
        return next((replace(link) for link in self._rows.values()
                     if link.target_url == target_url and link.owner_id == owner_id), None)

    async def list(self, owner_id: int | None = None) -> list[Link]:
        rows = [link for link in self._rows.values() if owner_id is None or link.owner_id == owner_id]
        return [replace(link) for link in sorted(rows, key=lambda link: link.id, reverse=True)]

    async def delete(self, link_id: int) -> None:
        self._rows.pop(link_id, None)
        clicks = self._db.clicks._rows
        for click_id in [c.id for c in clicks.values() if c.link_id == link_id]:
            del clicks[click_id]


class MemoryClickRepository(ClickRepository):
    def __init__(self) -> None:
        self._rows: dict[int, Click] = {}
        self._ids = count(1)

    async def add(self, link_id, *, clicked_at=None, ip=None, user_agent=None, referrer=None,
                  location=None) -> Click:
        loc = location or GeoLocation()
        click = Click(
            id=next(self._ids), link_id=link_id, clicked_at=clicked_at or _now(),
            ip=ip, user_agent=user_agent, referrer=referrer,
            country=loc.country, country_code=loc.country_code, region=loc.region, city=loc.city,
            latitude=loc.latitude, longitude=loc.longitude,
        )
        self._rows[click.id] = click
        return replace(click)

    async def set_location(self, click_id: int, location: GeoLocation) -> None:
        click = self._rows.get(click_id)
        if click is None:
            return
        click.country, click.country_code = location.country, location.country_code
        click.region, click.city = location.region, location.city
        click.latitude, click.longitude = location.latitude, location.longitude

    async def list_for_link(self, link_id: int) -> list[Click]:
        rows = [c for c in self._rows.values() if c.link_id == link_id]
        return [replace(c) for c in sorted(rows, key=lambda c: (c.clicked_at, c.id), reverse=True)]

    async def count_for_link(self, link_id: int) -> int:
        return sum(1 for c in self._rows.values() if c.link_id == link_id)

    async def count_by_link(self) -> dict[int, int]:
        counts: dict[int, int] = {}
        for click in self._rows.values():
            counts[click.link_id] = counts.get(click.link_id, 0) + 1
        return counts


class MemoryBlockRepository(BlockRepository):
    def __init__(self) -> None:
        self._rows: dict[str, IpBlock] = {}

    async def set(self, ip: str, reason: str, expires_at: datetime | None) -> IpBlock:
        block = self._rows[ip] = IpBlock(ip, reason, _now(), expires_at)
        return replace(block)

    async def delete(self, ip: str) -> None:
        self._rows.pop(ip, None)

    async def list(self) -> list[IpBlock]:
        return [replace(b) for b in self._rows.values()]


class MemoryDatabase(Database):
    def __init__(self) -> None:
        self.users = MemoryUserRepository(self)
        self.links = MemoryLinkRepository(self)
        self.clicks = MemoryClickRepository()
        self.blocks = MemoryBlockRepository()

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass
