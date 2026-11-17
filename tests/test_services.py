from datetime import UTC, date, datetime

import httpx
import pytest

from app.db import Click
from app.security import hash_password, verify_password
from app.services import qr
from app.services.analytics import record_click, summarize
from app.services.geoip import GeoResolver, IpApi, IpWhoIs, anonymize_ip
from app.services.shortener import (
    PNR_PATTERN, InvalidAliasError, InvalidUrlError, create_link, generate_code, normalize_url, resolve_code,
)


def test_password_hashing():
    stored = hash_password('s3cret')
    assert stored != hash_password('s3cret')  # salted
    assert verify_password('s3cret', stored)
    assert not verify_password('wrong', stored)
    assert not verify_password('s3cret', 'not-a-hash')


@pytest.mark.parametrize(('raw', 'expected'), [
    ('https://example.com/a?b=1', 'https://example.com/a?b=1'),
    ('  example.com/page ', 'https://example.com/page'),
    ('http://localhost:3000/x', 'http://localhost:3000/x'),
])
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


@pytest.mark.parametrize('raw', [
    '', '   ', 'javascript:alert(1)', 'ftp://example.com', 'https://', 'not a url', 'nodots',
    'https://example.com:port', 'https://example.com/' + 'a' * 2100,
])
def test_normalize_url_rejects(raw):
    with pytest.raises(InvalidUrlError):
        normalize_url(raw)


def test_generated_codes_look_like_pnrs():
    codes = {generate_code() for _ in range(500)}
    assert len(codes) == 500  # no repeats: the space is about a billion codes
    for code in codes:
        assert PNR_PATTERN.fullmatch(code), code
        assert len(code) == 6 and code == code.upper() and not set(code) & set('01IO')


async def test_codes_resolve_in_any_case_but_aliases_do_not(db):
    await db.links.add('K7QF2M', 'https://example.com')
    await db.links.add('Promo', 'https://example.org')
    assert (await resolve_code(db, 'K7QF2M')).code == 'K7QF2M'
    assert (await resolve_code(db, 'k7qf2m')).code == 'K7QF2M'
    assert (await resolve_code(db, 'Promo')).code == 'Promo'
    assert await resolve_code(db, 'promo') is None
    assert await resolve_code(db, 'zzzzzz') is None


async def test_create_link(db):
    owner = await db.users.add('sam', 'hash')
    link = await create_link(db, 'example.com', owner_id=owner.id)
    assert link.target_url == 'https://example.com' and PNR_PATTERN.fullmatch(link.code)

    # same URL, same owner: reused; different owner: separate link
    assert (await create_link(db, 'https://example.com', owner_id=owner.id)).code == link.code
    assert (await create_link(db, 'https://example.com')).code != link.code

    alias = await create_link(db, 'https://example.com', owner_id=owner.id, alias='my-link')
    assert alias.code == 'my-link'
    with pytest.raises(InvalidAliasError, match='taken'):
        await create_link(db, 'https://example.org', alias='my-link')
    for bad in ('ab', 'has space', 'login', 'API', 'sl/ash'):
        with pytest.raises(InvalidAliasError):
            await create_link(db, 'https://example.org', alias=bad)


def test_anonymize_ip():
    assert anonymize_ip('203.0.113.77') == '203.0.113.0'
    assert anonymize_ip('2001:db8:abcd:1234::1') == '2001:db8:abcd::'
    assert anonymize_ip('testclient') is None
    assert anonymize_ip(None) is None


async def test_geo_lookup_is_cached_and_skips_private_addresses(geo):
    location = await geo.locate('8.8.8.8')
    assert (location.city, location.latitude) == ('London', 51.5)
    await geo.locate('8.8.8.8')
    assert geo.calls == ['https://ipwho.is/8.8.8.8']

    for private in ('127.0.0.1', '192.168.1.5', '10.0.0.1', '::1', 'testclient', None):
        assert await geo.locate(private) is None
    assert len(geo.calls) == 1


async def test_geo_falls_back_to_the_next_provider():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == 'ipwho.is':
            return httpx.Response(200, json={'success': False, 'message': 'rate limited'})
        return httpx.Response(200, json={'status': 'success', 'country': 'Japan', 'countryCode': 'JP',
                                         'regionName': 'Tokyo', 'city': 'Tokyo', 'lat': 35.6, 'lon': 139.6})

    resolver = GeoResolver((IpWhoIs(), IpApi()), transport=httpx.MockTransport(handler))
    assert (await resolver.locate('8.8.8.8')).country == 'Japan'
    await resolver.close()


async def test_geo_survives_provider_outage():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError('down')

    resolver = GeoResolver(transport=httpx.MockTransport(handler))
    assert await resolver.locate('8.8.8.8') is None
    await resolver.close()


async def test_geo_local_fallback_uses_the_servers_own_location():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={'success': True, 'country': 'X', 'latitude': 1, 'longitude': 2})

    resolver = GeoResolver((IpWhoIs(),), local_fallback=True, transport=httpx.MockTransport(handler))
    assert (await resolver.locate('127.0.0.1')).latitude == 1.0
    assert seen == ['/']
    await resolver.close()


async def test_record_click_stores_location_and_truncated_ip(db, geo):
    link = await db.links.add('abc123', 'https://example.com')
    await record_click(db, geo, link.id, ip='8.8.8.8', user_agent='pytest', referrer='https://ref.example')
    await record_click(db, geo, link.id, ip='127.0.0.1')

    local, located = await db.clicks.list_for_link(link.id)
    assert (located.ip, located.city, located.country_code) == ('8.8.8.0', 'London', 'GB')
    assert located.user_agent == 'pytest' and located.referrer == 'https://ref.example'
    assert local.ip == '127.0.0.0' and not local.has_location


def test_summarize():
    def click(n, day, country=None, city=None, lat=None, lon=None):
        return Click(id=n, link_id=1, clicked_at=datetime(2026, 11, day, 12, tzinfo=UTC),
                     country=country, city=city, latitude=lat, longitude=lon)

    summary = summarize([
        click(1, 17, 'Japan', 'Tokyo', 35.68, 139.69),
        click(2, 17, 'Japan', 'Tokyo', 35.68, 139.69),
        click(3, 16, 'Kenya', 'Nairobi', -1.29, 36.82),
        click(4, 15),
    ], days=3, today=date(2026, 11, 17))

    assert (summary.total, summary.located) == (4, 3)
    assert summary.countries == [('Japan', 2), ('Kenya', 1)]
    assert [(p.label, p.clicks) for p in summary.places] == [('Tokyo, Japan', 2), ('Nairobi, Kenya', 1)]
    assert summary.daily == [(date(2026, 11, 15), 1), (date(2026, 11, 16), 1), (date(2026, 11, 17), 2)]
    assert summary.last_click == datetime(2026, 11, 17, 12, tzinfo=UTC)
    assert summarize([]).total == 0


def test_qr_codes():
    assert qr.svg('https://example.com/abc').startswith('<svg')
    assert qr.svg_data_uri('https://example.com/abc').startswith('data:image/svg+xml')
    assert qr.png('https://example.com/abc').startswith(b'\x89PNG')
