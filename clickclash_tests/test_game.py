from concurrent.futures import ThreadPoolExecutor
import io
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image

from trafficxx import clickclash
from trafficxx.db import connect
from trafficxx.clickclash_security import WORDS, verify
from conftest import signup


def test_guest_clicks_unranked_then_signup_preserves_score_and_hides_credentials(client):
    assert client.post('/api/click', json={'id': str(uuid4())}).json()['score'] == 1
    assert client.get('/api/leaderboard').json()['entries'] == []
    phrase = signup(client)
    assert len(phrase.split('-')) == 5
    assert all(word in WORDS for word in phrase.split('-'))
    me = client.get('/api/me').json()
    board = client.get('/api/leaderboard').json()
    assert board['entries'][0]['score'] == 1
    assert 'email' not in board['entries'][0]
    assert phrase not in str(board) and phrase not in str(me)
    with connect() as conn:
        row = conn.execute('SELECT * FROM clickclash.players WHERE id=%s', (me['id'],)).fetchone()
        assert row['password_hash'].startswith('$argon2id$') and verify(row['password_hash'], phrase)
        assert phrase not in str(row)


def test_parallel_retries_and_independent_clicks(client):
    click_id = str(uuid4())
    with ThreadPoolExecutor(max_workers=6) as workers:
        responses = list(workers.map(lambda _: client.post('/api/click', json={'id': click_id}), range(12)))
    assert all(r.status_code == 200 for r in responses)
    assert sum(not r.json()['duplicate'] for r in responses) == 1
    with ThreadPoolExecutor(max_workers=6) as workers:
        responses = list(workers.map(lambda _: client.post('/api/click', json={'id': str(uuid4())}), range(18)))
    assert all(r.status_code == 200 for r in responses)
    assert client.get('/api/me').json()['score'] == 19
    with connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM clickclash.receipts').fetchone()['n'] == 19


def test_another_player_cannot_claim_click_id(client):
    click_id = str(uuid4())
    assert client.post('/api/click', json={'id': click_id}).status_code == 200
    client.cookies.clear()
    assert client.post('/api/guest', json={}).status_code == 200
    client.headers['X-CC-CSRF'] = client.get('/api/me').json()['csrf']
    assert client.post('/api/click', json={'id': click_id}).status_code == 409
    assert client.get('/api/me').json()['score'] == 0


def test_restart_keeps_session_and_score(client):
    client.post('/api/click', json={'id': str(uuid4())})
    cookie = client.cookies.get('cc_session')
    clickclash.database.close()
    clickclash.database.open()
    assert client.cookies.get('cc_session') == cookie
    assert client.get('/api/me').json()['score'] == 1


def test_signup_collision_cannot_reset_password(client):
    phrase = signup(client)
    client.cookies.clear()
    client.post('/api/guest', json={})
    client.headers['X-CC-CSRF'] = client.get('/api/me').json()['csrf']
    response = client.post('/api/signup', json={'email': 'PLAYER@example.com', 'nickname': 'Other', 'accept_no_recovery': True})
    assert response.status_code == 409 and phrase not in response.text
    with connect() as conn:
        assert verify(conn.execute("SELECT password_hash FROM clickclash.players WHERE email='player@example.com'").fetchone()['password_hash'], phrase)


def test_login_atomically_merges_guest_score_and_retry_ownership(client):
    phrase = signup(client)
    client.post('/api/click', json={'id': str(uuid4())})
    assert client.post('/api/logout', json={}).status_code == 200
    client.post('/api/guest', json={})
    client.headers['X-CC-CSRF'] = client.get('/api/me').json()['csrf']
    guest_cookie = client.cookies.get('cc_session')
    click_id = str(uuid4())
    client.post('/api/click', json={'id': click_id})
    response = client.post('/api/login', json={'email': 'player@example.com', 'passphrase': phrase})
    assert response.status_code == 200
    client.headers['X-CC-CSRF'] = response.json()['csrf']
    assert client.get('/api/me').json()['score'] == 2
    assert client.cookies.get('cc_session') != guest_cookie
    assert client.post('/api/click', json={'id': click_id}).json()['duplicate'] is True
    assert client.get('/api/me').json()['score'] == 2


def test_origin_csrf_and_validation_do_not_echo_credentials(client):
    assert client.post('/api/click', json={'id': str(uuid4())}, headers={'Origin': 'https://evil.invalid'}).status_code == 403
    assert client.post('/api/click', json={'id': str(uuid4())}, headers={'X-CC-CSRF': 'wrong'}).status_code == 403
    secret = 'must-not-appear' * 30
    response = client.post('/api/login', json={'email': 'a@b.com', 'passphrase': secret})
    assert response.status_code == 422 and secret not in response.text
    assert client.get('/api/me').headers['Cache-Control'] == 'no-store'


def test_paused_and_global_budget_do_not_create_receipts(client):
    with connect() as conn:
        conn.execute('UPDATE clickclash.control SET paused=true WHERE id')
    response = client.post('/api/click', json={'id': str(uuid4())})
    assert response.status_code == 503 and response.json()['detail']['code'] == 'GAME_PAUSED'
    with connect() as conn:
        conn.execute('UPDATE clickclash.control SET paused=false WHERE id')
        conn.execute("INSERT INTO clickclash.buckets VALUES ('click-global',floor(extract(epoch FROM clock_timestamp()))::bigint,1000) ON CONFLICT DO NOTHING")
        # Insert the next few windows too, so this check is not clock-boundary flaky.
        conn.execute("INSERT INTO clickclash.buckets SELECT 'click-global',floor(extract(epoch FROM clock_timestamp()))::bigint+n,1000 FROM generate_series(1,3) n ON CONFLICT DO NOTHING")
    assert client.post('/api/click', json={'id': str(uuid4())}).status_code == 503
    assert client.get('/api/me').json()['score'] == 0


def test_capacity_shared_across_connections(client):
    with connect() as conn:
        settings = conn.execute('SELECT concurrent_writes FROM clickclash.control WHERE id').fetchone()
        for index in range(settings['concurrent_writes']):
            conn.execute('SELECT pg_advisory_xact_lock(%s)', (8432100 + index,))
        response = client.post('/api/click', json={'id': str(uuid4())})
        assert response.status_code == 503
    assert client.post('/api/click', json={'id': str(uuid4())}).status_code == 200


def test_profile_image_budget_moderation_and_link_validation(client):
    signup(client)
    me = client.get('/api/me').json()
    assert client.put('/api/profile', json={'nickname': 'Player', 'website': 'javascript:alert(1)'}).status_code == 422
    assert client.put('/api/profile', json={'nickname': 'Player', 'bio': '<script>literal text</script>', 'website': 'https://example.com'}).status_code == 200
    raw = io.BytesIO()
    Image.new('RGB', (40, 30), 'red').save(raw, format='PNG')
    assert client.put('/api/profile/image', content=raw.getvalue()).status_code == 200
    image = client.get('/api/images/' + me['id'])
    assert image.status_code == 200 and image.headers['Content-Type'] == 'image/webp'
    assert client.put('/api/profile/image', content=b'<svg onload="alert(1)"></svg>').status_code == 422
    with connect() as conn:
        assert conn.execute('SELECT image_bytes FROM clickclash.control WHERE id').fetchone()['image_bytes'] > 0
        conn.execute('UPDATE clickclash.players SET ad_hidden=true WHERE id=%s', (me['id'],))
    entry = client.get('/api/leaderboard').json()['entries'][0]
    assert entry['bio'] == '' and entry['website'] == '' and entry['image_url'] is None
    assert client.get('/api/images/' + me['id']).status_code == 404
    assert client.delete('/api/profile/image').status_code == 200
    with connect() as conn:
        assert conn.execute('SELECT image_bytes FROM clickclash.control WHERE id').fetchone()['image_bytes'] == 0


def test_expired_session_and_oversized_body(client):
    with connect() as conn:
        conn.execute("UPDATE clickclash.sessions SET expires_at=clock_timestamp()-interval '1 second'")
    assert client.get('/api/me').status_code == 401
    assert client.post('/api/login', content=b'x' * 9000).status_code == 413


def test_auth_limit_is_shared_and_not_a_click_cap(client):
    with connect() as conn:
        conn.execute("INSERT INTO clickclash.buckets VALUES ('auth-global',floor(extract(epoch FROM clock_timestamp())/60)::bigint*60,120) ON CONFLICT(key,bucket_start) DO UPDATE SET count=120")
    assert client.post('/api/login', json={'email': 'x@y.com', 'passphrase': 'wrong'}).status_code == 429
    assert client.post('/api/click', json={'id': str(uuid4())}).status_code == 200


def test_deletion_revokes_access_and_removes_public_profile(client):
    signup(client)
    assert client.delete('/api/account').status_code == 200
    assert client.get('/api/me').status_code == 401
    assert client.get('/api/leaderboard').json()['entries'] == []
    with connect() as conn:
        row = conn.execute('SELECT email,password_hash,bio,score FROM clickclash.players').fetchone()
        assert row == {'email': None, 'password_hash': None, 'bio': '', 'score': 0}


def test_click_payload_cannot_claim_extra_points(client):
    result = client.post('/api/click', json={'id': str(uuid4()), 'points': 1000})
    assert result.status_code == 422
    assert client.get('/api/me').json()['score'] == 0
