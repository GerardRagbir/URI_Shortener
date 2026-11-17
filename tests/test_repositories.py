"""Contract tests: every backend must behave the same (see the ``db`` fixture)."""
from datetime import UTC, datetime, timedelta

import pytest

from app.db import DuplicateError, GeoLocation, Role


async def test_users(db):
    admin = await db.users.add('root', 'hash', Role.ADMIN)
    user = await db.users.add('sam', 'hash')
    assert admin.is_admin and not user.is_admin
    assert (await db.users.get(user.id)).username == 'sam'
    assert (await db.users.get_by_username('root')).id == admin.id
    assert await db.users.get_by_username('nobody') is None
    assert [u.username for u in await db.users.list()] == ['root', 'sam']
    assert user.created_at.tzinfo is not None

    with pytest.raises(DuplicateError):
        await db.users.add('sam', 'other')

    await db.users.set_password(user.id, 'new-hash')
    assert (await db.users.get(user.id)).password_hash == 'new-hash'


async def test_links(db):
    owner = await db.users.add('sam', 'hash')
    first = await db.links.add('abc123', 'https://example.com', owner.id)
    second = await db.links.add('anon42', 'https://example.com')

    assert (await db.links.get_by_code('abc123')).target_url == 'https://example.com'
    assert await db.links.get_by_code('ABC123') is None  # codes are case-sensitive
    assert [link.code for link in await db.links.list()] == ['anon42', 'abc123']  # newest first
    assert [link.code for link in await db.links.list(owner.id)] == ['abc123']
    assert (await db.links.find_by_target('https://example.com', owner.id)).id == first.id
    assert (await db.links.find_by_target('https://example.com', None)).id == second.id
    assert await db.links.find_by_target('https://other.example', owner.id) is None

    with pytest.raises(DuplicateError):
        await db.links.add('abc123', 'https://elsewhere.example')


async def test_deleting_a_user_keeps_their_links(db):
    owner = await db.users.add('sam', 'hash')
    await db.links.add('abc123', 'https://example.com', owner.id)
    await db.users.delete(owner.id)
    assert await db.users.get(owner.id) is None
    assert (await db.links.get_by_code('abc123')).owner_id is None


async def test_clicks(db):
    link = await db.links.add('abc123', 'https://example.com')
    other = await db.links.add('other1', 'https://example.org')
    now = datetime.now(UTC)
    old = await db.clicks.add(link.id, clicked_at=now - timedelta(days=2), ip='203.0.113.0', user_agent='curl')
    await db.clicks.add(link.id, clicked_at=now, location=GeoLocation('Japan', 'JP', 'Tokyo', 'Tokyo', 35.6, 139.6))
    await db.clicks.add(other.id)

    clicks = await db.clicks.list_for_link(link.id)
    assert [c.country for c in clicks] == ['Japan', None]  # newest first
    assert clicks[0].has_location and not clicks[1].has_location
    assert clicks[1].ip == '203.0.113.0' and clicks[1].user_agent == 'curl'
    assert abs(clicks[1].clicked_at - (now - timedelta(days=2))) < timedelta(seconds=1)

    await db.clicks.set_location(old.id, GeoLocation('Kenya', 'KE', 'Nairobi', 'Nairobi', -1.29, 36.82))
    located = (await db.clicks.list_for_link(link.id))[1]
    assert (located.city, located.latitude, located.longitude) == ('Nairobi', -1.29, 36.82)

    assert await db.clicks.count_for_link(link.id) == 2
    assert await db.clicks.count_by_link() == {link.id: 2, other.id: 1}


async def test_deleting_a_link_removes_its_clicks(db):
    link = await db.links.add('abc123', 'https://example.com')
    keep = await db.links.add('keep99', 'https://example.org')
    await db.clicks.add(link.id)
    await db.clicks.add(keep.id)
    await db.links.delete(link.id)
    assert await db.links.get_by_code('abc123') is None
    assert await db.clicks.count_by_link() == {keep.id: 1}


async def test_blocks(db):
    expires = datetime.now(UTC) + timedelta(hours=1)
    block = await db.blocks.set('203.0.113.7', 'spam', expires)
    assert (block.ip, block.reason) == ('203.0.113.7', 'spam')
    assert abs(block.expires_at - expires) < timedelta(seconds=1)
    await db.blocks.set('2001:db8::', 'manual', None)

    await db.blocks.set('203.0.113.7', 'again', None)  # replaces, does not duplicate
    blocks = {b.ip: b for b in await db.blocks.list()}
    assert set(blocks) == {'203.0.113.7', '2001:db8::'}
    assert blocks['203.0.113.7'].reason == 'again' and blocks['203.0.113.7'].expires_at is None

    await db.blocks.delete('203.0.113.7')
    await db.blocks.delete('not-there')
    assert [b.ip for b in await db.blocks.list()] == ['2001:db8::']
