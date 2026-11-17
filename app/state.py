"""Process-wide services, created at startup in ``app.main.lifespan``."""
from fastapi import Request

from .config import get_settings
from .db import Database
from .services.geoip import GeoResolver
from .services.guard import Guard


class State:
    db: Database
    geo: GeoResolver
    guard: Guard


state = State()


def get_db() -> Database:
    return state.db


def public_origin(request: Request) -> str:
    """Origin that short links are printed with, without a trailing slash."""
    return (get_settings().base_url or str(request.base_url)).rstrip('/')


def client_ip(request: Request) -> str | None:
    if get_settings().trust_proxy_headers:
        if forwarded := request.headers.get('x-forwarded-for'):
            # The last entry is the one our own proxy appended; earlier ones are supplied by
            # the client and could be forged to dodge rate limits and blocks.
            return forwarded.split(',')[-1].strip()
    return request.client.host if request.client else None
