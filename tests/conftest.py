import os

# Must be set before ``app.main`` is imported: settings are read once at import time.
os.environ.update({
    'DATABASE_URL': 'memory://',
    'GEOIP_ENABLED': 'false',
    'STORAGE_SECRET': 'test-secret',
    'ADMIN_PASSWORD': 'admin-pass',
    'DEMO_PASSWORD': 'demo-pass',
    'BASE_URL': '',
})

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.db import create_database  # noqa: E402
from app.services.geoip import GeoResolver  # noqa: E402

LONDON = {'success': True, 'country': 'United Kingdom', 'country_code': 'GB', 'region': 'England',
          'city': 'London', 'latitude': 51.5, 'longitude': -0.12}


@pytest.fixture(params=['memory', 'sqlite'])
async def db(request, tmp_path):
    """Every storage backend; tests using this fixture run once per backend."""
    url = 'memory://' if request.param == 'memory' else f'sqlite:///{tmp_path / "test.db"}'
    database = create_database(url)
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
async def geo():
    """A resolver whose only provider is a stub that places every public IP in London."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=LONDON)

    resolver = GeoResolver(transport=httpx.MockTransport(handler))
    resolver.calls = calls
    yield resolver
    await resolver.close()
