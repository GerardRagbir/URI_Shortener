"""JSON API (HTTP Basic auth) and the public redirect endpoint."""
from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from .db import Database, Link, User
from .security import verify_password
from .services import qr
from .services.analytics import record_click, summarize
from .services.guard import Denied
from .services.shortener import InvalidAliasError, InvalidUrlError, create_link, resolve_code
from .state import client_ip, get_db, public_origin, state

router = APIRouter(prefix='/api')
basic = HTTPBasic(auto_error=False)

Db = Annotated[Database, Depends(get_db)]


async def authenticate(db: Database, username: str, password: str) -> User | None:
    user = await db.users.get_by_username(username)
    if user and verify_password(password, user.password_hash):
        return user
    return None


async def guarded(scope: str, request: Request, *, count: bool = True) -> None:
    """Apply a rate limit to the calling address, answering 429 or 403 when it is refused."""
    try:
        await state.guard.check(scope, client_ip(request), count=count)
    except Denied as e:
        raise HTTPException(e.status, e.message, headers=e.headers) from None


async def api_user(request: Request, db: Db,
                   credentials: Annotated[HTTPBasicCredentials | None, Depends(basic)]) -> User:
    await guarded('login', request, count=False)  # too many failed attempts: stop checking passwords
    user = credentials and await authenticate(db, credentials.username, credentials.password)
    if not user:
        if credentials:
            await state.guard.record('login', client_ip(request))
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, 'Invalid credentials',
                            headers={'WWW-Authenticate': 'Basic'})
    return user


ApiUser = Annotated[User, Depends(api_user)]


class LinkIn(BaseModel):
    url: str
    alias: str | None = None


class LinkOut(BaseModel):
    code: str
    short_url: str
    target_url: str
    qr_url: str
    created_at: datetime
    clicks: int


class PlaceOut(BaseModel):
    label: str
    latitude: float
    longitude: float
    clicks: int


class CountryOut(BaseModel):
    country: str
    clicks: int


class DayOut(BaseModel):
    day: date
    clicks: int


class StatsOut(BaseModel):
    code: str
    total_clicks: int
    located_clicks: int
    last_click: datetime | None
    countries: list[CountryOut]
    places: list[PlaceOut]
    daily: list[DayOut]


def link_out(link: Link, request: Request, clicks: int) -> LinkOut:
    origin = public_origin(request)
    return LinkOut(
        code=link.code, short_url=f'{origin}/{link.code}', target_url=link.target_url,
        qr_url=f'{origin}/api/links/{link.code}/qr', created_at=link.created_at, clicks=clicks,
    )


def can_view(user: User, link: Link) -> bool:
    return user.is_admin or link.owner_id == user.id


async def owned_link(code: str, db: Db, user: ApiUser) -> Link:
    link = await db.links.get_by_code(code)
    if not link or not can_view(user, link):  # same answer for both, so codes cannot be probed
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Link not found')
    return link


OwnedLink = Annotated[Link, Depends(owned_link)]


@router.get('/health', tags=['meta'])
async def health() -> dict[str, str]:
    return {'status': 'ok'}


@router.post('/links', status_code=status.HTTP_201_CREATED, tags=['links'])
async def create(body: LinkIn, request: Request, db: Db, user: ApiUser) -> LinkOut:
    """Shorten a URL. Re-submitting the same URL without an alias returns the existing link."""
    await guarded('create', request)
    try:
        link = await create_link(db, body.url, owner_id=user.id, alias=body.alias)
    except (InvalidUrlError, InvalidAliasError) as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from None
    return link_out(link, request, await db.clicks.count_for_link(link.id))


@router.get('/links', tags=['links'])
async def list_links(request: Request, db: Db, user: ApiUser) -> list[LinkOut]:
    """Your links, newest first. Administrators see everyone's."""
    links = await db.links.list(None if user.is_admin else user.id)
    counts = await db.clicks.count_by_link()
    return [link_out(link, request, counts.get(link.id, 0)) for link in links]


@router.get('/links/{code}', tags=['links'])
async def get_link(link: OwnedLink, request: Request, db: Db) -> LinkOut:
    return link_out(link, request, await db.clicks.count_for_link(link.id))


@router.delete('/links/{code}', status_code=status.HTTP_204_NO_CONTENT, tags=['links'])
async def delete_link(link: OwnedLink, db: Db) -> None:
    await db.links.delete(link.id)


@router.get('/links/{code}/stats', tags=['links'])
async def stats(link: OwnedLink, db: Db) -> StatsOut:
    """Click totals, per-day counts and the geographic breakdown."""
    summary = summarize(await db.clicks.list_for_link(link.id))
    return StatsOut(
        code=link.code, total_clicks=summary.total, located_clicks=summary.located,
        last_click=summary.last_click,
        countries=[CountryOut(country=name, clicks=n) for name, n in summary.countries],
        places=[PlaceOut(label=p.label, latitude=p.latitude, longitude=p.longitude, clicks=p.clicks)
                for p in summary.places],
        daily=[DayOut(day=day, clicks=n) for day, n in summary.daily],
    )


@router.get('/links/{code}/qr', tags=['links'], response_class=Response,
            responses={200: {'content': {'image/svg+xml': {}, 'image/png': {}}}})
async def qr_code(code: str, request: Request, db: Db,
                  format: Literal['svg', 'png'] = 'svg', download: bool = False) -> Response:
    """QR code of the short URL. Public, like the short URL itself."""
    if not await db.links.get_by_code(code):
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'Link not found')
    short_url = f'{public_origin(request)}/{code}'
    if format == 'png':
        response = Response(qr.png(short_url), media_type='image/png')
    else:
        response = Response(qr.svg(short_url), media_type='image/svg+xml')
    if download:
        response.headers['Content-Disposition'] = f'attachment; filename="{code}-qr.{format}"'
    return response


async def follow_link(code: str, request: Request, background: BackgroundTasks) -> RedirectResponse:
    """``GET /{code}``: redirect to the destination and record the click.

    The click (including its geolocation lookup) is processed after the response
    is sent, so visitors are never kept waiting. A 307 keeps browsers from
    caching the redirect, which would hide repeat clicks.
    """
    await guarded('redirect', request)
    link = await resolve_code(state.db, code)
    if not link:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'This short link does not exist.')
    background.add_task(
        record_click, state.db, state.geo, link.id,
        ip=client_ip(request),
        user_agent=request.headers.get('user-agent'),
        referrer=request.headers.get('referer'),
    )
    return RedirectResponse(link.target_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
