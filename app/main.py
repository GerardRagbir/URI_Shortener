"""The ASGI application: ``uvicorn app.main:app``, or ``python -m app`` to use the configured host and port."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from nicegui import app as nicegui_app
from nicegui import ui

from .api import follow_link, router
from .config import DEV_STORAGE_SECRET, get_settings
from .db import create_database
from .services.geoip import GeoResolver
from .services.guard import HOUR, Denied, Guard, default_rules
from .services.seed import seed
from .state import client_ip, state
from .ui import pages  # noqa: F401 - importing registers the pages

log = logging.getLogger('uvicorn.error')
settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.storage_secret == DEV_STORAGE_SECRET:
        log.warning('STORAGE_SECRET is not set; using the insecure development default.')
    state.db = create_database(settings.database_url)
    await state.db.connect()
    state.geo = GeoResolver(
        enabled=settings.geoip_enabled,
        timeout=settings.geoip_timeout,
        local_fallback=settings.geoip_local_fallback,
    )
    state.guard = Guard(
        state.db,
        default_rules(
            requests=settings.rate_limit_requests,
            redirects=settings.rate_limit_redirects,
            links=settings.rate_limit_links,
            links_daily=settings.rate_limit_links_daily,
            logins=settings.rate_limit_logins,
        ),
        enabled=settings.rate_limit_enabled,
        strikes=settings.auto_block_strikes,
        block_seconds=settings.auto_block_hours * HOUR,
    )
    await state.guard.load()
    await seed(state.db, settings)
    yield
    await state.geo.close()
    await state.db.close()


app = FastAPI(
    title='µrl',
    description='Shorten URLs, get QR codes, and see where the clicks come from.',
    version='2.0.0',
    lifespan=lifespan,
)
app.include_router(router)


class GuardMiddleware:
    """Turn away blocked addresses and enforce the overall per-IP request limit.

    Runs before anything else, so a refused request costs next to nothing. The
    framework's own static assets are not counted: a single page view loads many.
    """

    def __init__(self, asgi_app) -> None:
        self.app = asgi_app

    async def __call__(self, scope, receive, send) -> None:
        guard = getattr(state, 'guard', None)
        if scope['type'] != 'http' or guard is None:
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        try:
            await guard.check('request', client_ip(request), count=not request.url.path.startswith('/_nicegui/'))
        except Denied as e:
            await PlainTextResponse(e.message, e.status, headers=e.headers)(scope, receive, send)
            return
        await self.app(scope, receive, send)


app.add_middleware(GuardMiddleware)

# Short links live at the root ("/abc123"), next to the UI pages. The route is added
# after the pages so those win; ``RESERVED_CODES`` keeps codes from colliding with them.
nicegui_app.add_api_route('/{code}', follow_link, methods=['GET'], include_in_schema=False)

ui.run_with(
    app,
    title='µrl',
    favicon='🔗',
    storage_secret=settings.storage_secret,
    show_welcome_message=False,
)

