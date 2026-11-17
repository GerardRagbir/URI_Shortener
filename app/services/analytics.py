"""Recording clicks and summarising them for display."""
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from ..db import Click, Database
from .geoip import GeoResolver, anonymize_ip


async def record_click(
    db: Database,
    geo: GeoResolver,
    link_id: int,
    *,
    ip: str | None,
    user_agent: str | None = None,
    referrer: str | None = None,
    clicked_at: datetime | None = None,
) -> Click:
    """Store a click, then geolocate it.

    The click is saved first so it is never lost to a slow or failing lookup.
    Only a truncated IP address is kept.
    """
    click = await db.clicks.add(
        link_id, clicked_at=clicked_at, ip=anonymize_ip(ip), user_agent=user_agent, referrer=referrer)
    location = await geo.locate(ip)
    if location is not None:
        await db.clicks.set_location(click.id, location)
    return click


@dataclass(slots=True)
class Place:
    """Clicks that share a map position."""
    latitude: float
    longitude: float
    label: str
    clicks: int


@dataclass(slots=True)
class Summary:
    total: int = 0
    located: int = 0
    last_click: datetime | None = None
    countries: list[tuple[str, int]] = field(default_factory=list)  # most clicks first
    places: list[Place] = field(default_factory=list)  # most clicks first
    daily: list[tuple[date, int]] = field(default_factory=list)  # oldest first, no gaps


def place_label(click: Click) -> str:
    parts = [p for p in (click.city, click.region, click.country) if p]
    # "Singapore, Singapore, Singapore" helps nobody
    return ', '.join(dict.fromkeys(parts)) or 'Unknown location'


def summarize(clicks: list[Click], *, days: int = 14, today: date | None = None) -> Summary:
    today = today or datetime.now(UTC).date()
    countries: Counter[str] = Counter()
    places: dict[tuple[float, float], Place] = {}
    per_day: Counter[date] = Counter()

    for click in clicks:
        per_day[click.clicked_at.astimezone(UTC).date()] += 1
        if click.country:
            countries[click.country] += 1
        if click.has_location:
            key = (round(click.latitude, 2), round(click.longitude, 2))
            if key in places:
                places[key].clicks += 1
            else:
                places[key] = Place(click.latitude, click.longitude, place_label(click), 1)

    window = [today - timedelta(days=n) for n in range(days - 1, -1, -1)]
    return Summary(
        total=len(clicks),
        located=sum(1 for c in clicks if c.has_location),
        last_click=max((c.clicked_at for c in clicks), default=None),
        countries=countries.most_common(),
        places=sorted(places.values(), key=lambda p: p.clicks, reverse=True),
        daily=[(day, per_day[day]) for day in window],
    )
