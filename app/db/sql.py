"""SQL backend built on SQLAlchemy's async engine.

Works with any database SQLAlchemy has an async driver for (SQLite, PostgreSQL,
MySQL/MariaDB, ...). The ORM classes below are private to this module; callers
only ever see the dataclasses from ``app.db.models``.
"""
from datetime import UTC, datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, String, Text, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .base import BlockRepository, ClickRepository, Database, DuplicateError, LinkRepository, UserRepository
from .models import Click, GeoLocation, IpBlock, Link, Role, User

# Lets people write the familiar short URL form; the async driver is filled in.
ASYNC_DRIVERS = {
    'sqlite': 'sqlite+aiosqlite',
    'postgres': 'postgresql+asyncpg',
    'postgresql': 'postgresql+asyncpg',
    'mysql': 'mysql+aiomysql',
    'mariadb': 'mysql+aiomysql',
}


def normalize_url(url: str) -> str:
    scheme, sep, rest = url.partition('://')
    return f'{ASYNC_DRIVERS.get(scheme, scheme)}{sep}{rest}'


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime) -> datetime:
    """Some databases (SQLite, MySQL) drop the timezone; everything is stored as UTC."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    pass


class UserRow(Base):
    __tablename__ = 'users'

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def to_model(self) -> User:
        return User(self.id, self.username, self.password_hash, Role(self.role), _utc(self.created_at))


class LinkRow(Base):
    __tablename__ = 'links'

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    target_url: Mapped[str] = mapped_column(Text)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey('users.id'), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def to_model(self) -> Link:
        return Link(self.id, self.code, self.target_url, self.owner_id, _utc(self.created_at))


class ClickRow(Base):
    __tablename__ = 'clicks'
    __table_args__ = (Index('ix_clicks_link_time', 'link_id', 'clicked_at'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    link_id: Mapped[int] = mapped_column(ForeignKey('links.id'))
    clicked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    referrer: Mapped[str | None] = mapped_column(String(512))
    country: Mapped[str | None] = mapped_column(String(128))
    country_code: Mapped[str | None] = mapped_column(String(8))
    region: Mapped[str | None] = mapped_column(String(128))
    city: Mapped[str | None] = mapped_column(String(128))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)

    def to_model(self) -> Click:
        return Click(
            id=self.id, link_id=self.link_id, clicked_at=_utc(self.clicked_at),
            ip=self.ip, user_agent=self.user_agent, referrer=self.referrer,
            country=self.country, country_code=self.country_code, region=self.region, city=self.city,
            latitude=self.latitude, longitude=self.longitude,
        )


class BlockRow(Base):
    __tablename__ = 'ip_blocks'

    ip: Mapped[str] = mapped_column(String(64), primary_key=True)
    reason: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def to_model(self) -> IpBlock:
        return IpBlock(self.ip, self.reason, _utc(self.created_at),
                       _utc(self.expires_at) if self.expires_at else None)


def _clip(value: str | None, length: int) -> str | None:
    return value[:length] if value else value


class _SqlRepository:
    def __init__(self, sessions: async_sessionmaker) -> None:
        self._sessions = sessions


class SqlUserRepository(_SqlRepository, UserRepository):
    async def add(self, username: str, password_hash: str, role: Role = Role.USER) -> User:
        row = UserRow(username=username, password_hash=password_hash, role=Role(role).value, created_at=_now())
        async with self._sessions() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as e:
                raise DuplicateError(f'username {username!r} already exists') from e
            return row.to_model()

    async def get(self, user_id: int) -> User | None:
        async with self._sessions() as session:
            row = await session.get(UserRow, user_id)
            return row.to_model() if row else None

    async def get_by_username(self, username: str) -> User | None:
        async with self._sessions() as session:
            row = await session.scalar(select(UserRow).where(UserRow.username == username))
            return row.to_model() if row else None

    async def list(self) -> list[User]:
        async with self._sessions() as session:
            rows = await session.scalars(select(UserRow).order_by(UserRow.id))
            return [row.to_model() for row in rows]

    async def set_password(self, user_id: int, password_hash: str) -> None:
        async with self._sessions() as session:
            await session.execute(update(UserRow).where(UserRow.id == user_id).values(password_hash=password_hash))
            await session.commit()

    async def delete(self, user_id: int) -> None:
        async with self._sessions() as session:
            await session.execute(update(LinkRow).where(LinkRow.owner_id == user_id).values(owner_id=None))
            await session.execute(delete(UserRow).where(UserRow.id == user_id))
            await session.commit()


class SqlLinkRepository(_SqlRepository, LinkRepository):
    async def add(self, code: str, target_url: str, owner_id: int | None = None) -> Link:
        row = LinkRow(code=code, target_url=target_url, owner_id=owner_id, created_at=_now())
        async with self._sessions() as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as e:
                raise DuplicateError(f'code {code!r} already exists') from e
            return row.to_model()

    async def get_by_code(self, code: str) -> Link | None:
        async with self._sessions() as session:
            row = await session.scalar(select(LinkRow).where(LinkRow.code == code))
            # Some databases compare strings case-insensitively by default; codes are case-sensitive.
            return row.to_model() if row and row.code == code else None

    async def find_by_target(self, target_url: str, owner_id: int | None) -> Link | None:
        owner = LinkRow.owner_id.is_(None) if owner_id is None else LinkRow.owner_id == owner_id
        async with self._sessions() as session:
            row = await session.scalar(
                select(LinkRow).where(LinkRow.target_url == target_url, owner).order_by(LinkRow.id).limit(1))
            return row.to_model() if row else None

    async def list(self, owner_id: int | None = None) -> list[Link]:
        query = select(LinkRow).order_by(LinkRow.id.desc())
        if owner_id is not None:
            query = query.where(LinkRow.owner_id == owner_id)
        async with self._sessions() as session:
            return [row.to_model() for row in await session.scalars(query)]

    async def delete(self, link_id: int) -> None:
        async with self._sessions() as session:
            await session.execute(delete(ClickRow).where(ClickRow.link_id == link_id))
            await session.execute(delete(LinkRow).where(LinkRow.id == link_id))
            await session.commit()


class SqlClickRepository(_SqlRepository, ClickRepository):
    async def add(self, link_id, *, clicked_at=None, ip=None, user_agent=None, referrer=None,
                  location=None) -> Click:
        loc = location or GeoLocation()
        row = ClickRow(
            link_id=link_id, clicked_at=clicked_at or _now(), ip=_clip(ip, 64),
            user_agent=_clip(user_agent, 512), referrer=_clip(referrer, 512),
            country=_clip(loc.country, 128), country_code=_clip(loc.country_code, 8),
            region=_clip(loc.region, 128), city=_clip(loc.city, 128),
            latitude=loc.latitude, longitude=loc.longitude,
        )
        async with self._sessions() as session:
            session.add(row)
            await session.commit()
            return row.to_model()

    async def set_location(self, click_id: int, location: GeoLocation) -> None:
        async with self._sessions() as session:
            await session.execute(update(ClickRow).where(ClickRow.id == click_id).values(
                country=_clip(location.country, 128), country_code=_clip(location.country_code, 8),
                region=_clip(location.region, 128), city=_clip(location.city, 128),
                latitude=location.latitude, longitude=location.longitude,
            ))
            await session.commit()

    async def list_for_link(self, link_id: int) -> list[Click]:
        query = (select(ClickRow).where(ClickRow.link_id == link_id)
                 .order_by(ClickRow.clicked_at.desc(), ClickRow.id.desc()))
        async with self._sessions() as session:
            return [row.to_model() for row in await session.scalars(query)]

    async def count_for_link(self, link_id: int) -> int:
        async with self._sessions() as session:
            return await session.scalar(
                select(func.count()).select_from(ClickRow).where(ClickRow.link_id == link_id)) or 0

    async def count_by_link(self) -> dict[int, int]:
        async with self._sessions() as session:
            rows = await session.execute(select(ClickRow.link_id, func.count()).group_by(ClickRow.link_id))
            return {link_id: n for link_id, n in rows}


class SqlBlockRepository(_SqlRepository, BlockRepository):
    async def set(self, ip: str, reason: str, expires_at: datetime | None) -> IpBlock:
        async with self._sessions() as session:
            row = await session.get(BlockRow, ip)
            if row is None:
                row = BlockRow(ip=ip)
                session.add(row)
            row.reason, row.created_at, row.expires_at = reason[:255], _now(), expires_at
            await session.commit()
            return row.to_model()

    async def delete(self, ip: str) -> None:
        async with self._sessions() as session:
            await session.execute(delete(BlockRow).where(BlockRow.ip == ip))
            await session.commit()

    async def list(self) -> list[IpBlock]:
        async with self._sessions() as session:
            return [row.to_model() for row in await session.scalars(select(BlockRow))]


class SqlDatabase(Database):
    def __init__(self, url: str) -> None:
        self.url = normalize_url(url)
        self._engine: AsyncEngine = create_async_engine(self.url, pool_pre_ping=True)
        sessions = async_sessionmaker(self._engine, expire_on_commit=False)
        self.users = SqlUserRepository(sessions)
        self.links = SqlLinkRepository(sessions)
        self.clicks = SqlClickRepository(sessions)
        self.blocks = SqlBlockRepository(sessions)

    async def connect(self) -> None:
        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self._engine.dispose()
