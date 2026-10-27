"""Only a disposable, loopback PostgreSQL database is accepted."""
import os
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from trafficxx.db import connect
from trafficxx import clickclash
from trafficxx.clickclash_migrate import run


@pytest.fixture(scope='session', autouse=True)
def schema():
    url = os.environ.get('CLICKCLASH_TEST_DATABASE_URL', '')
    parsed = urlsplit(url)
    if parsed.hostname not in ('127.0.0.1', 'localhost') or parsed.path != '/clickclash_test':
        pytest.fail('CLICKCLASH_TEST_DATABASE_URL must name the disposable loopback clickclash_test DB')
    os.environ['DATABASE_URL'] = url
    os.environ['DATABASE_AUTH_MODE'] = 'url'
    os.environ['CLICKCLASH_ORIGIN'] = 'https://clickclash.test'
    os.environ.pop('CLICKCLASH_INSECURE_LOCAL', None)
    run()


@pytest.fixture(autouse=True)
def clean(schema):
    with connect() as conn:
        conn.execute('TRUNCATE clickclash.image_blobs,clickclash.reports,clickclash.receipts,clickclash.images,clickclash.sessions,'
                     'clickclash.players,clickclash.buckets,clickclash.operator_audit CASCADE')
        conn.execute('UPDATE clickclash.control SET paused=false,requests_per_second=1000,concurrent_writes=16,image_bytes=0 WHERE id')
    yield
    with connect() as conn:
        conn.execute('DELETE FROM clickclash.buckets')
        conn.execute('UPDATE clickclash.control SET paused=false,requests_per_second=120,concurrent_writes=16 WHERE id')


@pytest.fixture
def client():
    with TestClient(clickclash.app, base_url='https://clickclash.test') as browser:
        browser.headers['Origin'] = 'https://clickclash.test'
        assert browser.post('/api/guest', json={}).status_code == 200
        browser.headers['X-CC-CSRF'] = browser.get('/api/me').json()['csrf']
        yield browser


def signup(client, address='player@example.com', nickname='Player'):
    result = client.post('/api/signup', json={'email': address, 'nickname': nickname, 'accept_no_recovery': True})
    assert result.status_code == 201, result.text
    client.headers['X-CC-CSRF'] = result.json()['csrf']
    return result.json()['passphrase']
