"""End-to-end tests through the real ASGI app (FastAPI + mounted NiceGUI)."""
import re

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.state import state

DEMO = ('demo', 'demo-pass')
ADMIN = ('admin', 'admin-pass')


@pytest.fixture(scope='module')
def client():
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


def test_default_users_and_demo_link_are_seeded(client):
    assert client.get('/api/links', auth=ADMIN).status_code == 200
    links = client.get('/api/links', auth=DEMO).json()
    assert [link['code'] for link in links] == ['demo']
    assert links[0]['clicks'] == 80


def test_api_requires_valid_credentials(client):
    assert client.get('/api/links').status_code == 401
    assert client.get('/api/links', auth=('demo', 'wrong')).status_code == 401
    assert client.post('/api/links', json={'url': 'https://example.com'}).status_code == 401


def test_shorten_redirect_and_stats(client):
    created = client.post('/api/links', json={'url': 'example.com/landing'}, auth=DEMO)
    assert created.status_code == 201
    link = created.json()
    assert link['target_url'] == 'https://example.com/landing'
    assert link['short_url'] == f'http://testserver/{link["code"]}'

    for _ in range(2):
        response = client.get(f'/{link["code"]}', headers={'referer': 'https://ref.example'})
        assert response.status_code == 307
        assert response.headers['location'] == 'https://example.com/landing'
    assert re.fullmatch(r'[A-HJ-NP-Z2-9]{6}', link['code'])  # PNR-style
    assert client.get(f'/{link["code"].lower()}').status_code == 307  # case-insensitive, like a PNR

    stats = client.get(f'/api/links/{link["code"]}/stats', auth=DEMO).json()
    assert stats['total_clicks'] == 3 and stats['located_clicks'] == 0
    assert client.get(f'/api/links/{link["code"]}', auth=DEMO).json()['clicks'] == 3


def test_alias_and_validation_errors(client):
    assert client.post('/api/links', json={'url': 'https://example.org', 'alias': 'docs-link'},
                       auth=DEMO).json()['code'] == 'docs-link'
    for body in ({'url': 'ftp://example.org'}, {'url': 'https://example.org', 'alias': 'docs-link'},
                 {'url': 'https://example.org', 'alias': 'login'}):
        assert client.post('/api/links', json=body, auth=DEMO).status_code == 422


def test_links_are_private_to_their_owner_but_visible_to_admins(client):
    code = client.post('/api/links', json={'url': 'https://admin-only.example'}, auth=ADMIN).json()['code']
    assert client.get(f'/api/links/{code}/stats', auth=DEMO).status_code == 404
    assert client.delete(f'/api/links/{code}', auth=DEMO).status_code == 404
    assert code not in [link['code'] for link in client.get('/api/links', auth=DEMO).json()]
    assert 'demo' in [link['code'] for link in client.get('/api/links', auth=ADMIN).json()]

    assert client.delete(f'/api/links/{code}', auth=ADMIN).status_code == 204
    assert client.get(f'/{code}').status_code == 404


def test_qr_code_endpoint(client):
    svg = client.get('/api/links/demo/qr')
    assert svg.headers['content-type'] == 'image/svg+xml' and svg.text.startswith('<svg')
    png = client.get('/api/links/demo/qr?format=png&download=true')
    assert png.content.startswith(b'\x89PNG')
    assert png.headers['content-disposition'] == 'attachment; filename="demo-qr.png"'
    assert client.get('/api/links/missing/qr').status_code == 404


def test_pages_coexist_with_short_codes(client):
    assert client.get('/').status_code == 200
    assert client.get('/login').status_code == 200
    assert client.get('/docs').status_code == 200
    for protected in ('/links', '/links/demo', '/admin/users'):
        response = client.get(protected)
        assert response.status_code == 307 and response.headers['location'] == f'/login?next={protected}'
    assert client.get('/no-such-code').status_code == 404


async def test_seeding_is_idempotent(client):
    from app.config import get_settings
    from app.services.seed import seed
    await seed(state.db, get_settings())
    assert len(await state.db.users.list()) == 2


async def test_blocked_address_is_refused_everywhere(client):
    await state.guard.block('testclient', 'test', 3600)
    try:
        for path in ('/', '/demo', '/api/health', '/login'):
            response = client.get(path)
            assert response.status_code == 403 and 'blocked' in response.text
    finally:
        await state.guard.unblock('testclient')
    assert client.get('/api/health').status_code == 200


def test_link_creation_is_rate_limited(client):
    rule = state.guard.rules['create'][0]
    statuses = [client.post('/api/links', json={'url': f'https://example.com/{n}'}, auth=DEMO).status_code
                for n in range(rule.limit + 1)]
    assert statuses[-1] == 429 and 201 in statuses
    refused = client.post('/api/links', json={'url': 'https://example.com/more'}, auth=DEMO)
    assert int(refused.headers['retry-after']) > 0
    state.guard._windows.clear()


def test_repeated_failed_logins_are_locked_out(client):
    limit = state.guard.rules['login'][0].limit
    for _ in range(limit):
        assert client.get('/api/links', auth=('demo', 'wrong')).status_code == 401
    assert client.get('/api/links', auth=('demo', 'wrong')).status_code == 429
    assert client.get('/api/links', auth=DEMO).status_code == 429  # even with the right password
    state.guard._windows.clear()
    assert client.get('/api/links', auth=DEMO).status_code == 200
