import pytest

from app.db import create_database
from app.services.guard import DAY, HOUR, MINUTE, Denied, Guard, Rule, ip_key

RULES = {
    'request': [Rule(3, MINUTE)],
    'create': [Rule(2, MINUTE), Rule(3, DAY)],
    'login': [Rule(2, 10 * MINUTE)],
}
PUBLIC, OTHER = '8.8.8.8', '9.9.9.9'


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def guard(db, clock):
    return Guard(db, RULES, strikes=2, strike_window=HOUR, block_seconds=DAY, clock=clock)


async def refused(guard, scope, ip, **kwargs) -> Denied | None:
    try:
        await guard.check(scope, ip, **kwargs)
    except Denied as e:
        return e
    return None


def test_ip_key():
    assert ip_key(' 8.8.8.8 ') == '8.8.8.8'
    assert ip_key('2001:db8:1:2:aaaa::1') == ip_key('2001:db8:1:2:bbbb::9') == '2001:db8:1:2::'
    assert ip_key('::ffff:8.8.8.8') == '8.8.8.8'
    assert ip_key('testclient') == 'testclient'
    assert ip_key(None) is None and ip_key('') is None


async def test_limit_is_per_address_and_resets_after_the_window(guard, clock):
    for _ in range(3):
        assert await refused(guard, 'request', PUBLIC) is None
    denied = await refused(guard, 'request', PUBLIC)
    assert denied.status == 429 and 0 < denied.retry_after <= 61
    assert denied.headers == {'Retry-After': str(denied.retry_after)}
    assert await refused(guard, 'request', OTHER) is None  # someone else is unaffected

    clock.now += 61
    assert await refused(guard, 'request', PUBLIC) is None


async def test_long_window_outlasts_the_short_one(guard, clock):
    for _ in range(3):  # 2 per minute, 3 per day
        await refused(guard, 'create', '10.0.0.1')
        clock.now += 61
    denied = await refused(guard, 'create', '10.0.0.1')
    assert denied.status == 429 and denied.retry_after > HOUR


async def test_failed_logins_lock_out_without_counting_successes(guard):
    for _ in range(5):  # checking alone never uses up the budget
        assert await refused(guard, 'login', PUBLIC, count=False) is None
    await guard.record('login', PUBLIC)
    assert await refused(guard, 'login', PUBLIC, count=False) is None
    await guard.record('login', PUBLIC)
    assert (await refused(guard, 'login', PUBLIC, count=False)).status == 429


async def test_repeat_offenders_are_blocked_and_the_block_is_persisted(guard, clock, db):
    for _ in range(4):
        await refused(guard, 'request', PUBLIC)  # first strike
    assert guard.active_block(PUBLIC) is None
    clock.now += 61
    for _ in range(3):
        await refused(guard, 'request', PUBLIC)
    assert (await refused(guard, 'request', PUBLIC)).status == 403  # second strike: blocked
    assert (await refused(guard, 'create', PUBLIC)).status == 403  # for everything
    assert [b.ip for b in await db.blocks.list()] == [PUBLIC]

    # a restarted process still knows about it ...
    restarted = Guard(db, RULES, clock=clock)
    await restarted.load()
    assert (await refused(restarted, 'request', PUBLIC)).status == 403

    # ... until it expires
    clock.now += DAY + 1
    assert await refused(restarted, 'request', PUBLIC) is None
    await restarted.load()
    assert await db.blocks.list() == []


async def test_strikes_expire(guard, clock):
    for _ in range(4):
        await refused(guard, 'request', PUBLIC)
    clock.now += HOUR + 61
    for _ in range(4):
        await refused(guard, 'request', PUBLIC)
    assert guard.active_block(PUBLIC) is None


async def test_private_addresses_are_limited_but_never_auto_blocked(guard, clock):
    for _ in range(5):
        for _ in range(4):
            await refused(guard, 'request', '127.0.0.1')
        clock.now += 61
    assert guard.active_block('127.0.0.1') is None
    for _ in range(3):
        await refused(guard, 'request', '127.0.0.1')
    assert (await refused(guard, 'request', '127.0.0.1')).status == 429


async def test_manual_block_and_unblock(guard, clock):
    await guard.block('2001:db8:1:2::5', 'abuse report', None)
    assert (await refused(guard, 'request', '2001:db8:1:2:ffff::1')).status == 403  # whole /64
    assert [b.reason for b in guard.blocks()] == ['abuse report']
    clock.now += 365 * DAY
    assert guard.active_block('2001:db8:1:2::5') is not None  # permanent

    await guard.unblock('2001:db8:1:2::5')
    assert await refused(guard, 'request', '2001:db8:1:2::5') is None
    assert guard.blocks() == []


async def test_disabled_guard_still_enforces_blocks():
    db = create_database('memory://')
    guard = Guard(db, RULES, enabled=False)
    for _ in range(10):
        assert await refused(guard, 'request', PUBLIC) is None
    await guard.block(PUBLIC, 'manual', HOUR)
    assert (await refused(guard, 'request', PUBLIC)).status == 403
