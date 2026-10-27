from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import io
from pathlib import Path
from uuid import uuid4

from PIL import Image
import psycopg
import pytest

from trafficxx import clickclash
from trafficxx.db import connect
from conftest import signup


def release():
    spec = importlib.util.spec_from_file_location('cc_release', Path('scripts/clickclash-azure.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_role_can_use_actual_api_but_not_operator_controls(client, monkeypatch):
    sql = release().registration_sql({'principalId': str(uuid4()), 'tenantId': str(uuid4())})
    grants = 'GRANT USAGE ON SCHEMA clickclash' + sql.split('GRANT USAGE ON SCHEMA clickclash', 1)[1].split('DO $scope$', 1)[0]
    with connect() as conn:
        conn.execute("DO $$ BEGIN IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='trafficxx-lab-clickclash') THEN CREATE ROLE \"trafficxx-lab-clickclash\" NOLOGIN; END IF; END $$")
        conn.execute(grants)
    original = clickclash.database.connection
    @contextmanager
    def restricted():
        with original() as conn:
            conn.execute('SET LOCAL ROLE "trafficxx-lab-clickclash"')
            yield conn
    monkeypatch.setattr(clickclash.database, 'connection', restricted)
    signup(client)
    player_id = client.get('/api/me').json()['id']
    assert client.post('/api/click', json={'id': str(uuid4())}).status_code == 200
    raw = io.BytesIO()
    Image.new('RGB', (20, 20)).save(raw, format='PNG')
    assert client.put('/api/profile/image', content=raw.getvalue()).status_code == 200
    assert client.post('/api/report/' + player_id, json={'reason': 'Test report'}).status_code == 200
    assert client.get('/api/leaderboard').status_code == 200
    assert client.delete('/api/account').status_code == 200
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with restricted() as conn:
            conn.execute('UPDATE clickclash.control SET paused=true WHERE id')
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with restricted() as conn:
            conn.execute('CREATE TABLE clickclash.not_allowed(id int)')


def test_shared_bucket_does_not_oversubscribe_under_race():
    def attempt(_):
        with connect() as conn:
            return clickclash.bucket(conn, 'shared-race', 3600, 3)
    with ThreadPoolExecutor(max_workers=8) as workers:
        assert sum(workers.map(attempt, range(16))) == 3


def test_rendered_release_keeps_ticket_services_private():
    value = release().deployment('clickclash.example.com', '20.1.2.3', {'clientId': str(uuid4()), 'tenantId': str(uuid4())},
                                 'example.azurecr.io/trafficxx@sha256:' + 'a' * 64)
    objects = value['items']
    services = {item['metadata']['name']: item['spec'] for item in objects if item['kind'] == 'Service'}
    assert set(services) == {'clickclash', 'clickclash-public'}
    assert 'type' not in services['clickclash']
    assert services['clickclash-public']['selector'] == {'app': 'clickclash-gateway'}
    assert [p['port'] for p in services['clickclash-public']['ports']] == [80, 443]
    assert not any('booking' in item['metadata']['name'] for item in objects)
    app = next(item for item in objects if item['kind'] == 'Deployment' and item['metadata']['name'] == 'clickclash')
    assert app['spec']['replicas'] == 2
    container = app['spec']['template']['spec']['containers'][0]
    assert container['securityContext']['readOnlyRootFilesystem'] is True
    assert 'DATABASE_URL' not in {setting['name'] for setting in container['env']}
