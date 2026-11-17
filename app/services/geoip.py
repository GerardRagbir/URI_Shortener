"""IP geolocation through free, keyless lookup services.

Providers are tried in order until one answers, and results are cached so a
visitor is only looked up once. Every provider is a small class, so adding or
reordering them is a one-line change in ``DEFAULT_PROVIDERS``.
"""
import ipaddress
import logging
from abc import ABC, abstractmethod
from collections import OrderedDict

import httpx

from ..db import GeoLocation

log = logging.getLogger(__name__)


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class GeoProvider(ABC):
    name: str

    @abstractmethod
    def url(self, ip: str | None) -> str:
        """Lookup URL for ``ip``; ``None`` asks for the caller's own location."""

    @abstractmethod
    def parse(self, data: dict) -> GeoLocation | None: ...

    async def lookup(self, client: httpx.AsyncClient, ip: str | None) -> GeoLocation | None:
        response = await client.get(self.url(ip))
        response.raise_for_status()
        location = self.parse(response.json())
        if location is None or location.latitude is None or location.longitude is None:
            return None
        return location


class IpWhoIs(GeoProvider):
    name = 'ipwho.is'

    def url(self, ip):
        return f'https://ipwho.is/{ip or ""}'

    def parse(self, data):
        if not data.get('success'):
            return None
        return GeoLocation(
            country=data.get('country'), country_code=data.get('country_code'),
            region=data.get('region'), city=data.get('city'),
            latitude=_float(data.get('latitude')), longitude=_float(data.get('longitude')),
        )


class GeoJs(GeoProvider):
    name = 'geojs.io'

    def url(self, ip):
        return f'https://get.geojs.io/v1/ip/geo/{ip}.json' if ip else 'https://get.geojs.io/v1/ip/geo.json'

    def parse(self, data):
        return GeoLocation(
            country=data.get('country'), country_code=data.get('country_code'),
            region=data.get('region'), city=data.get('city'),
            latitude=_float(data.get('latitude')), longitude=_float(data.get('longitude')),
        )


class IpApi(GeoProvider):
    """Last resort: the free tier of ip-api.com is served over plain HTTP only."""
    name = 'ip-api.com'

    def url(self, ip):
        return f'http://ip-api.com/json/{ip or ""}?fields=status,country,countryCode,regionName,city,lat,lon'

    def parse(self, data):
        if data.get('status') != 'success':
            return None
        return GeoLocation(
            country=data.get('country'), country_code=data.get('countryCode'),
            region=data.get('regionName'), city=data.get('city'),
            latitude=_float(data.get('lat')), longitude=_float(data.get('lon')),
        )


DEFAULT_PROVIDERS: tuple[GeoProvider, ...] = (IpWhoIs(), GeoJs(), IpApi())


def is_public_ip(ip: str | None) -> bool:
    try:
        return ipaddress.ip_address(ip or '').is_global
    except ValueError:
        return False


def anonymize_ip(ip: str | None) -> str | None:
    """Truncate an address before it is stored (IPv4 to /24, IPv6 to /48)."""
    try:
        address = ipaddress.ip_address(ip or '')
    except ValueError:
        return None
    prefix = 24 if address.version == 4 else 48
    return str(ipaddress.ip_network(f'{address}/{prefix}', strict=False).network_address)


class GeoResolver:
    def __init__(
        self,
        providers: tuple[GeoProvider, ...] = DEFAULT_PROVIDERS,
        *,
        enabled: bool = True,
        timeout: float = 4.0,
        local_fallback: bool = False,
        cache_size: int = 2048,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.providers = providers
        self.enabled = enabled
        self.local_fallback = local_fallback
        self._cache: OrderedDict[str | None, GeoLocation] = OrderedDict()
        self._cache_size = cache_size
        self._client = httpx.AsyncClient(
            timeout=timeout, transport=transport, headers={'User-Agent': 'url-shortener'})

    async def close(self) -> None:
        await self._client.aclose()

    async def locate(self, ip: str | None) -> GeoLocation | None:
        """Return the location of ``ip``, or ``None`` if it cannot be determined."""
        if not self.enabled:
            return None
        key: str | None
        if is_public_ip(ip):
            key = ip
        elif self.local_fallback:
            key = None  # private address: fall back to the server's own public location
        else:
            return None

        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        for provider in self.providers:
            try:
                location = await provider.lookup(self._client, key)
            except (httpx.HTTPError, ValueError) as e:
                log.warning('geolocation via %s failed: %s', provider.name, type(e).__name__)
                continue
            if location is not None:
                self._cache[key] = location
                if len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)
                return location
        return None
