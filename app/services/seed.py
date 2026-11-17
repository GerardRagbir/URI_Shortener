"""Default accounts and sample data, created on first start."""
import logging
import random
from datetime import UTC, datetime, timedelta

from ..config import Settings
from ..db import Database, GeoLocation, Role
from ..security import hash_password

log = logging.getLogger(__name__)

DEMO_ALIAS = 'demo'
DEMO_TARGET = 'https://www.openstreetmap.org'

# (city, region, country, code, latitude, longitude, relative weight)
SAMPLE_PLACES = [
    ('Port of Spain', 'Port of Spain', 'Trinidad and Tobago', 'TT', 10.6596, -61.5019, 9),
    ('London', 'England', 'United Kingdom', 'GB', 51.5074, -0.1278, 7),
    ('New York', 'New York', 'United States', 'US', 40.7128, -74.0060, 7),
    ('San Francisco', 'California', 'United States', 'US', 37.7749, -122.4194, 4),
    ('Toronto', 'Ontario', 'Canada', 'CA', 43.6532, -79.3832, 4),
    ('Berlin', 'Berlin', 'Germany', 'DE', 52.5200, 13.4050, 3),
    ('Lagos', 'Lagos', 'Nigeria', 'NG', 6.5244, 3.3792, 3),
    ('São Paulo', 'São Paulo', 'Brazil', 'BR', -23.5505, -46.6333, 3),
    ('Bengaluru', 'Karnataka', 'India', 'IN', 12.9716, 77.5946, 3),
    ('Singapore', 'Singapore', 'Singapore', 'SG', 1.3521, 103.8198, 2),
    ('Tokyo', 'Tokyo', 'Japan', 'JP', 35.6762, 139.6503, 2),
    ('Sydney', 'New South Wales', 'Australia', 'AU', -33.8688, 151.2093, 2),
]


async def ensure_user(db: Database, username: str, password: str, role: Role) -> tuple[int, bool]:
    """Create the user if missing. Returns ``(user id, created)``."""
    if existing := await db.users.get_by_username(username):
        return existing.id, False
    user = await db.users.add(username, hash_password(password), role)
    log.info('created default %s account "%s"', role.value, username)
    return user.id, True


async def seed(db: Database, settings: Settings) -> None:
    await ensure_user(db, settings.admin_username, settings.admin_password, Role.ADMIN)
    demo_id, created = await ensure_user(db, settings.demo_username, settings.demo_password, Role.USER)
    if created and settings.seed_demo_data:
        await seed_demo_link(db, demo_id)


async def seed_demo_link(db: Database, owner_id: int, *, clicks: int = 80) -> None:
    """Give the demo account one link with made-up clicks, so the map is not empty on first run."""
    if await db.links.get_by_code(DEMO_ALIAS):
        return
    link = await db.links.add(DEMO_ALIAS, DEMO_TARGET, owner_id)
    rng = random.Random(7)
    now = datetime.now(UTC)
    weights = [place[-1] for place in SAMPLE_PLACES]
    for _ in range(clicks):
        city, region, country, code, lat, lon, _ = rng.choices(SAMPLE_PLACES, weights)[0]
        days_ago = min(int(rng.expovariate(0.25)), 13)  # busier towards today
        await db.clicks.add(
            link.id,
            clicked_at=now - timedelta(days=days_ago, minutes=rng.randrange(1, 1440)),
            user_agent='sample data',
            location=GeoLocation(country, code, region, city, lat, lon),
        )
