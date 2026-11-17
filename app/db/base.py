"""The storage contract.

The application talks only to these abstract classes. To support a new kind of
database, implement ``Database`` and its four repositories, then register the
URL scheme in ``app.db.create_database``. ``tests/test_repositories.py`` runs the
same contract tests against every backend.
"""
from abc import ABC, abstractmethod
from datetime import datetime

from .models import Click, GeoLocation, IpBlock, Link, Role, User


class DuplicateError(Exception):
    """Raised when a unique value (username, short code) already exists."""


class UserRepository(ABC):
    @abstractmethod
    async def add(self, username: str, password_hash: str, role: Role = Role.USER) -> User: ...

    @abstractmethod
    async def get(self, user_id: int) -> User | None: ...

    @abstractmethod
    async def get_by_username(self, username: str) -> User | None: ...

    @abstractmethod
    async def list(self) -> list[User]: ...

    @abstractmethod
    async def set_password(self, user_id: int, password_hash: str) -> None: ...

    @abstractmethod
    async def delete(self, user_id: int) -> None:
        """Delete a user. Their links are kept and become ownerless."""


class LinkRepository(ABC):
    @abstractmethod
    async def add(self, code: str, target_url: str, owner_id: int | None = None) -> Link: ...

    @abstractmethod
    async def get_by_code(self, code: str) -> Link | None: ...

    @abstractmethod
    async def find_by_target(self, target_url: str, owner_id: int | None) -> Link | None:
        """Return an existing link to ``target_url`` with exactly this owner, if any."""

    @abstractmethod
    async def list(self, owner_id: int | None = None) -> list[Link]:
        """List links, newest first. ``owner_id=None`` lists everyone's links."""

    @abstractmethod
    async def delete(self, link_id: int) -> None:
        """Delete a link together with its clicks."""


class ClickRepository(ABC):
    @abstractmethod
    async def add(
        self,
        link_id: int,
        *,
        clicked_at: datetime | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
        referrer: str | None = None,
        location: GeoLocation | None = None,
    ) -> Click: ...

    @abstractmethod
    async def set_location(self, click_id: int, location: GeoLocation) -> None: ...

    @abstractmethod
    async def list_for_link(self, link_id: int) -> list[Click]:
        """List a link's clicks, newest first."""

    @abstractmethod
    async def count_for_link(self, link_id: int) -> int: ...

    @abstractmethod
    async def count_by_link(self) -> dict[int, int]:
        """Return ``{link_id: number of clicks}`` for every link that has clicks."""


class BlockRepository(ABC):
    @abstractmethod
    async def set(self, ip: str, reason: str, expires_at: datetime | None) -> IpBlock:
        """Block ``ip``, replacing any existing block on it."""

    @abstractmethod
    async def delete(self, ip: str) -> None: ...

    @abstractmethod
    async def list(self) -> list[IpBlock]: ...


class Database(ABC):
    """A connected store exposing one repository per entity."""

    users: UserRepository
    links: LinkRepository
    clicks: ClickRepository
    blocks: BlockRepository

    @abstractmethod
    async def connect(self) -> None:
        """Open the connection and make sure the schema exists."""

    @abstractmethod
    async def close(self) -> None: ...
