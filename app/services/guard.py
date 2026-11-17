"""Abuse protection: per-IP rate limits and an IP block list.

Every address gets a request budget per scope (see ``default_rules``). Going over
a budget answers 429 until the window ends and counts as one strike. An address
that keeps collecting strikes is blocked outright for a while, and the block is
stored in the database so it survives restarts. Administrators can also block
and unblock addresses by hand.
"""
import ipaddress
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..db import Database, IpBlock

log = logging.getLogger('uvicorn.error')

MINUTE, HOUR, DAY = 60, 3600, 86400


@dataclass(frozen=True, slots=True)
class Rule:
    limit: int  # requests allowed ...
    window: int  # ... per this many seconds


class Denied(Exception):
    """The request must be refused: 429 when over a limit, 403 when the address is blocked."""

    def __init__(self, status: int, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after = retry_after

    @property
    def headers(self) -> dict[str, str]:
        return {'Retry-After': str(self.retry_after)} if self.retry_after else {}


def ip_key(ip: str | None) -> str | None:
    """The identity limits are counted against.

    An IPv6 customer usually controls a whole /64, so those are treated as one
    client; otherwise rotating through the range would sidestep every limit.
    """
    if not ip:
        return None
    try:
        address = ipaddress.ip_address(ip.strip())
    except ValueError:
        return ip.strip().lower()
    if address.version == 6:
        if address.ipv4_mapped:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network(f'{address}/64', strict=False).network_address)
    return str(address)


def is_public(key: str) -> bool:
    try:
        return ipaddress.ip_address(key).is_global
    except ValueError:
        return False


def default_rules(*, requests: int, redirects: int, links: int, links_daily: int, logins: int) -> dict[str, list[Rule]]:
    return {
        'request': [Rule(requests, MINUTE)],  # any page or API call
        'redirect': [Rule(redirects, MINUTE)],  # following short links
        'create': [Rule(links, MINUTE), Rule(links_daily, DAY)],  # shortening
        'login': [Rule(logins, 10 * MINUTE)],  # failed sign-ins only
    }


class Guard:
    def __init__(
        self,
        db: Database,
        rules: dict[str, list[Rule]],
        *,
        enabled: bool = True,
        strikes: int = 5,
        strike_window: int = HOUR,
        block_seconds: int = DAY,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db = db
        self.rules = rules
        self.enabled = enabled
        self.strikes = strikes
        self.strike_window = strike_window
        self.block_seconds = block_seconds
        self._clock = clock
        self._windows: dict[tuple[str, int, str], list[float]] = {}  # -> [window start, count]
        self._strikes: dict[str, deque[float]] = {}
        self._blocks: dict[str, IpBlock] = {}

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self._clock(), UTC)

    # ------------------------------------------------------------------ block list

    async def load(self) -> None:
        """Read the stored blocks, dropping the ones that have run out."""
        self._blocks.clear()
        for block in await self.db.blocks.list():
            if block.expires_at and block.expires_at <= self._now():
                await self.db.blocks.delete(block.ip)
            else:
                self._blocks[block.ip] = block

    def active_block(self, ip: str | None) -> IpBlock | None:
        key = ip_key(ip)
        block = self._blocks.get(key) if key else None
        if block and block.expires_at and block.expires_at <= self._now():
            del self._blocks[key]
            return None
        return block

    def blocks(self) -> list[IpBlock]:
        now = self._now()
        active = [b for b in self._blocks.values() if not b.expires_at or b.expires_at > now]
        return sorted(active, key=lambda b: b.created_at, reverse=True)

    async def block(self, ip: str, reason: str, seconds: int | None) -> IpBlock:
        """Block an address for ``seconds``, or until unblocked when ``None``."""
        key = ip_key(ip)
        if not key:
            raise ValueError('an address is required')
        expires = self._now() + timedelta(seconds=seconds) if seconds else None
        block = await self.db.blocks.set(key, reason, expires)
        self._blocks[key] = block
        return block

    async def unblock(self, ip: str) -> None:
        key = ip_key(ip)
        if key:
            self._blocks.pop(key, None)
            self._strikes.pop(key, None)
            await self.db.blocks.delete(key)

    # ------------------------------------------------------------------ rate limits

    def _refuse_if_blocked(self, ip: str | None) -> None:
        if block := self.active_block(ip):
            until = f'until {block.expires_at:%Y-%m-%d %H:%M} UTC' if block.expires_at else 'indefinitely'
            raise Denied(403, f'Access from your address is blocked {until}.')

    def _prune(self, now: float) -> None:
        longest = max(rule.window for rules in self.rules.values() for rule in rules)
        self._windows = {k: v for k, v in self._windows.items() if now - v[0] < longest}
        self._strikes = {k: v for k, v in self._strikes.items() if v and now - v[-1] < self.strike_window}

    async def check(self, scope: str, ip: str | None, *, count: bool = True) -> None:
        """Count one request against ``scope`` and raise ``Denied`` if it is not allowed.

        With ``count=False`` nothing is counted; the call only refuses addresses
        that are blocked or have already used up their budget.
        """
        self._refuse_if_blocked(ip)
        key = ip_key(ip)
        if not self.enabled or not key:
            return
        now = self._clock()
        if len(self._windows) > 50_000:
            self._prune(now)

        retry_after = 0
        for index, rule in enumerate(self.rules.get(scope, ())):
            window = self._windows.get((scope, index, key))
            if window is None or now - window[0] >= rule.window:
                window = self._windows[(scope, index, key)] = [now, 0]
            if count:
                window[1] += 1
                if window[1] == rule.limit + 1:  # first refusal in this window
                    await self._strike(key, scope, now)
            if window[1] > rule.limit or (not count and window[1] >= rule.limit):
                retry_after = max(retry_after, int(window[0] + rule.window - now) + 1)
        self._refuse_if_blocked(ip)  # the strike above may just have blocked the address
        if retry_after:
            raise Denied(429, 'Too many requests. Please slow down and try again later.', retry_after)

    async def record(self, scope: str, ip: str | None) -> None:
        """Count an event without refusing the current request (used for failed sign-ins)."""
        try:
            await self.check(scope, ip)
        except Denied:
            pass

    async def _strike(self, key: str, scope: str, now: float) -> None:
        # Shared and unroutable addresses (localhost, a misconfigured proxy) are rate
        # limited but never blocked automatically: that would lock out everyone behind them.
        if not is_public(key) or self.strikes <= 0:
            return
        strikes = self._strikes.setdefault(key, deque())
        strikes.append(now)
        while strikes and now - strikes[0] > self.strike_window:
            strikes.popleft()
        if len(strikes) >= self.strikes:
            strikes.clear()
            await self.block(key, f'Automatic: repeatedly exceeded the {scope} rate limit', self.block_seconds)
            log.warning('blocked %s for %d hours after repeated rate limit violations', key, self.block_seconds // HOUR)
