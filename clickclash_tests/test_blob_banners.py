import hashlib
import io
from uuid import uuid4

from PIL import Image
import pytest

from trafficxx import clickclash, clickclash_blobs as blobs
from trafficxx.clickclash_blob_maintenance import migrate, verify
from trafficxx.db import connect
from conftest import signup


@pytest.fixture
def storage(monkeypatch):
    objects = {}
    monkeypatch.setenv('CLICKCLASH_BLOB_ACCOUNT_URL', 'https://fixture.blob.core.windows.net')
    def put(player, data):
        digest = hashlib.sha256(data).hexdigest()
        key = blobs.name(player, digest)
        objects[key] = data
        return key, digest
    def read(key, player, digest):
        if key != blobs.name(player, digest) or key not in objects:
            raise blobs.StorageUnavailable()
        return objects[key]
    monkeypatch.setattr(blobs, 'put', put)
    monkeypatch.setattr(blobs, 'read', read)
    from unittest.mock import MagicMock
    monkeypatch.setattr(blobs, 'container', lambda: MagicMock())
    return objects


def picture(color='red'):
    raw = io.BytesIO()
    Image.new('RGB', (60, 10), color).save(raw, format='PNG')
    return raw.getvalue()


def test_blob_upload_retry_replace_moderate_and_delete(client, storage):
    signup(client)
    player = client.get('/api/me').json()['id']
    image_urls = []
    for color in ('red', 'red', 'blue'):
        assert client.put('/api/profile/image', content=picture(color)).status_code == 200
        image_urls.append(client.get('/api/leaderboard').json()['entries'][0]['image_url'])
    assert image_urls[0] == image_urls[1] and image_urls[1] != image_urls[2]
    with connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM clickclash.images').fetchone()['n'] == 0
        row = conn.execute('SELECT * FROM clickclash.image_blobs').fetchone()
        assert conn.execute('SELECT image_bytes FROM clickclash.control').fetchone()['image_bytes'] == row['byte_size']
    assert len(storage) == 2  # Previous immutable object awaits safe orphan cleanup.
    assert client.get('/api/leaderboard').json()['entries'][0]['image_url']
    assert client.get('/api/images/' + player).content == storage[row['blob_name']]
    with connect() as conn:
        conn.execute('UPDATE clickclash.players SET ad_hidden=true WHERE id=%s', (player,))
    assert client.get('/api/images/' + player).status_code == 404
    assert client.get('/api/leaderboard').json()['entries'][0]['image_url'] is None
    assert client.delete('/api/profile/image').status_code == 200
    with connect() as conn:
        assert conn.execute('SELECT image_bytes FROM clickclash.control').fetchone()['image_bytes'] == 0


def test_storage_failure_retains_previous_banner_and_budget(client, storage, monkeypatch):
    signup(client)
    assert client.put('/api/profile/image', content=picture()).status_code == 200
    before = client.get('/api/images/' + client.get('/api/me').json()['id']).content
    def broken(*args):
        raise blobs.StorageUnavailable('secret diagnostic must not be public')
    monkeypatch.setattr(blobs, 'put', broken)
    response = client.put('/api/profile/image', content=picture('blue'))
    assert response.status_code == 503 and 'secret' not in response.text
    assert client.get('/api/images/' + client.get('/api/me').json()['id']).content == before
    with connect() as conn:
        row = conn.execute('SELECT * FROM clickclash.image_blobs').fetchone()
        assert conn.execute('SELECT image_bytes FROM clickclash.control').fetchone()['image_bytes'] == row['byte_size']


def test_migration_is_resumable_and_preserves_image_on_failure(client, storage, monkeypatch):
    signup(client)
    monkeypatch.delenv('CLICKCLASH_BLOB_ACCOUNT_URL')
    assert client.put('/api/profile/image', content=picture()).status_code == 200
    monkeypatch.setenv('CLICKCLASH_BLOB_ACCOUNT_URL', 'https://fixture.blob.core.windows.net')
    original = blobs.put
    def broken(*args):
        raise blobs.StorageUnavailable()
    monkeypatch.setattr(blobs, 'put', broken)
    with pytest.raises(blobs.StorageUnavailable):
        migrate()
    with connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM clickclash.images').fetchone()['n'] == 1
    monkeypatch.setattr(blobs, 'put', original)
    assert migrate()['moved'] == 1
    assert migrate()['moved'] == 0
    assert verify()['verified'] == 1
    assert client.delete('/api/account').status_code == 200
    with connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM clickclash.image_blobs').fetchone()['n'] == 0
        assert conn.execute('SELECT image_bytes FROM clickclash.control').fetchone()['image_bytes'] == 0


def test_invalid_upload_or_csrf_never_calls_storage(client, storage):
    signup(client)
    assert client.put('/api/profile/image', content=b'<svg onload="alert(1)"/>').status_code == 422
    assert client.put('/api/profile/image', content=picture(), headers={'X-CC-CSRF': 'wrong'}).status_code == 403
    assert client.put('/api/profile/image', content=picture(), headers={'Origin': 'https://evil.test'}).status_code == 403
    assert not storage


def test_revoked_session_cannot_complete_login_after_password_check(client, monkeypatch):
    phrase = signup(client)
    client.post('/api/logout')
    client.post('/api/guest', json={})
    client.headers['X-CC-CSRF'] = client.get('/api/me').json()['csrf']
    original = clickclash.security.verify
    def revoke(encoded, plaintext):
        accepted = original(encoded, plaintext)
        with connect() as conn:
            conn.execute('DELETE FROM clickclash.sessions')
        return accepted
    monkeypatch.setattr(clickclash.security, 'verify', revoke)
    assert client.post('/api/login', json={'email': 'player@example.com', 'passphrase': phrase}).status_code == 401
    assert client.get('/api/me').status_code == 401


def test_non_ascii_csrf_is_rejected_without_server_error(client):
    response = client.post('/api/click', json={'id': str(uuid4())}, headers={b'X-CC-CSRF': b'\xff'})
    assert response.status_code == 403


def test_image_decode_concurrency_is_bounded_and_released(client):
    signup(client)
    clickclash.IMAGE_SLOTS.acquire()
    clickclash.IMAGE_SLOTS.acquire()
    try:
        response = client.put('/api/profile/image', content=picture())
        assert response.status_code == 503 and response.json()['detail']['code'] == 'IMAGE_UPLOAD_BUSY'
    finally:
        clickclash.IMAGE_SLOTS.release()
        clickclash.IMAGE_SLOTS.release()
    assert client.put('/api/profile/image', content=picture()).status_code == 200


def test_budget_and_missing_blob_configuration_fail_closed(client, storage, monkeypatch):
    signup(client)
    with connect() as conn:
        conn.execute('UPDATE clickclash.control SET image_bytes=104857600 WHERE id')
    assert client.put('/api/profile/image', content=picture()).status_code == 503
    assert not storage
    with connect() as conn:
        conn.execute('UPDATE clickclash.control SET image_bytes=0 WHERE id')
    assert client.put('/api/profile/image', content=picture()).status_code == 200
    monkeypatch.delenv('CLICKCLASH_BLOB_ACCOUNT_URL')
    assert client.put('/api/profile/image', content=picture('blue')).status_code == 503
    with connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM clickclash.images').fetchone()['n'] == 0
        assert conn.execute('SELECT count(*) AS n FROM clickclash.image_blobs').fetchone()['n'] == 1


def test_concurrent_migration_and_replacement_keep_budget_consistent(client, storage, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    signup(client)
    monkeypatch.delenv('CLICKCLASH_BLOB_ACCOUNT_URL')
    assert client.put('/api/profile/image', content=picture()).status_code == 200
    monkeypatch.setenv('CLICKCLASH_BLOB_ACCOUNT_URL', 'https://fixture.blob.core.windows.net')
    with ThreadPoolExecutor(max_workers=2) as workers:
        migrating = workers.submit(migrate)
        replacing = workers.submit(client.put, '/api/profile/image', content=picture('blue'))
        assert replacing.result().status_code == 200
        assert migrating.result()['remaining'] == 0
    with connect() as conn:
        row = conn.execute('SELECT * FROM clickclash.image_blobs').fetchone()
        assert conn.execute('SELECT image_bytes FROM clickclash.control').fetchone()['image_bytes'] == row['byte_size']
    assert verify()['verified'] == 1


def test_database_failure_after_blob_copy_does_not_publish_it(client, storage, monkeypatch):
    import psycopg
    signup(client)
    assert client.put('/api/profile/image', content=picture()).status_code == 200
    before = client.get('/api/leaderboard').json()['entries'][0]['image_url']
    original = blobs.put
    def copied_then_database_failed(*args):
        original(*args)
        raise psycopg.OperationalError('private diagnostic')
    monkeypatch.setattr(blobs, 'put', copied_then_database_failed)
    response = client.put('/api/profile/image', content=picture('blue'))
    assert response.status_code == 503 and 'private diagnostic' not in response.text
    assert len(storage) == 2
    assert client.get('/api/leaderboard').json()['entries'][0]['image_url'] == before


def test_missing_private_object_returns_sanitized_503(client, storage):
    signup(client)
    assert client.put('/api/profile/image', content=picture()).status_code == 200
    url = client.get('/api/leaderboard').json()['entries'][0]['image_url']
    storage.clear()
    response = client.get(url)
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'IMAGE_STORAGE_UNAVAILABLE'


def test_cleanup_preserves_current_and_recent_objects(client, storage, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from trafficxx.clickclash_blob_maintenance import cleanup
    signup(client)
    player = client.get('/api/me').json()['id']
    assert client.put('/api/profile/image', content=picture()).status_code == 200
    old_key = next(iter(storage))
    assert client.put('/api/profile/image', content=picture('blue')).status_code == 200
    current_key = next(key for key in storage if key != old_key)
    recent_key, _ = blobs.put(player, b'recent-orphan')
    now = datetime.now(timezone.utc)
    rows = [SimpleNamespace(name=key, last_modified=now-timedelta(days=16) if key != recent_key else now)
            for key in (old_key, current_key, recent_key)]
    class Pages:
        continuation_token = None
        def __init__(self): self.pages = iter([rows])
        def __iter__(self): return self
        def __next__(self): return next(self.pages)
    container = MagicMock()
    container.list_blobs.return_value.by_page.side_effect = lambda **kwargs: Pages()
    monkeypatch.setattr(blobs, 'container', lambda: container)
    assert cleanup()['candidates'] == 1
    container.get_blob_client.assert_not_called()
    assert cleanup(execute=True)['removed'] == 1
    container.get_blob_client.assert_called_once_with(old_key)
