"""Database abstraction layer.

    db = create_database('sqlite:///./urls.db')   # or postgresql://..., mysql://..., memory://
    await db.connect()
    link = await db.links.get_by_code('abc123')

Application code depends on ``Database`` and the dataclasses in ``models`` only.
"""
from .base import BlockRepository, ClickRepository, Database, DuplicateError, LinkRepository, UserRepository
from .models import Click, GeoLocation, IpBlock, Link, Role, User

__all__ = [
    'BlockRepository', 'Click', 'ClickRepository', 'Database', 'DuplicateError', 'GeoLocation', 'IpBlock', 'Link',
    'LinkRepository',
    'Role', 'User', 'UserRepository', 'create_database',
]


def create_database(url: str) -> Database:
    """Pick a backend from the URL scheme. New backends are registered here."""
    scheme = url.partition('://')[0].partition('+')[0]
    if scheme == 'memory':
        from .memory import MemoryDatabase
        return MemoryDatabase()
    # Everything else is handed to SQLAlchemy, which reports unknown dialects itself.
    from .sql import SqlDatabase
    return SqlDatabase(url)
