"""ClickClash: shared PostgreSQL scoring and server-side browser sessions."""
from contextlib import asynccontextmanager
import hashlib
import ipaddress
import os
from pathlib import Path
import secrets
from threading import BoundedSemaphore
from uuid import UUID, uuid4

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from typing import Literal
from psycopg import Error, errors

from trafficxx.database_pool import DatabasePool
from trafficxx import clickclash_security as security
from trafficxx import clickclash_blobs as blobs

database = DatabasePool('clickclash', 'CLICKCLASH_POOL_MAX')
IMAGE_SLOTS = BoundedSemaphore(2)
COOKIE = 'cc_session'
DUMMY_HASH = security.hasher.hash(secrets.token_urlsafe(32))
DIST = Path(__file__).resolve().parent.parent / 'clickclash-web' / 'dist'


def fail(code, status=400):
    raise HTTPException(status, detail={'code': code}, headers={'Retry-After': '1'} if status in (429, 503) else None)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def public_player(row):
    return {key: row[key] for key in ('id', 'nickname', 'score', 'bio', 'website', 'ad_hidden')} | {
        'account': row['email'] is not None, 'image_url': f"/api/images/{row['id']}" if row.get('has_image') else None}


def board_player(row):
    row = dict(row)
    visible = row.pop('has_image')
    version = row.pop('image_digest')
    row['image_url'] = f"/api/images/{row['id']}?v={version}" if visible else None
    return row


def session(conn, request, write=False):
    token = request.cookies.get(COOKIE, '')
    if not token or len(token) > 100:
        fail('SESSION_REQUIRED', 401)
    row = conn.execute('SELECT s.csrf, p.* FROM clickclash.sessions s '
                       'JOIN clickclash.players p ON p.id=s.player_id '
                       'WHERE s.token_hash=%s AND s.expires_at > clock_timestamp()', (digest(token),)).fetchone()
    if not row or row['merged_into'] or row['banned']:
        fail('SESSION_EXPIRED', 401)
    if write:
        csrf = request.headers.get('x-cc-csrf', '')
        if not csrf.isascii() or len(csrf) > 100 or not secrets.compare_digest(csrf, row['csrf']):
            fail('CSRF_INVALID', 403)
    return row


def issue(conn, player_id, response):
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    conn.execute("INSERT INTO clickclash.sessions VALUES (%s,%s,%s,clock_timestamp()+interval '30 days')",
                 (digest(token), player_id, csrf))
    response.set_cookie(COOKIE, token, max_age=2592000, httponly=True, secure=os.environ.get('CLICKCLASH_INSECURE_LOCAL', '') != '1',
                        samesite='lax', path='/')
    return csrf


def bucket(conn, key, seconds, limit):
    # One short committed transaction per admission, shared across every replica.
    row = conn.execute('INSERT INTO clickclash.buckets(key,bucket_start,count) '
                       'VALUES (%s, floor(extract(epoch FROM clock_timestamp())/%s)::bigint * %s, 1) '
                       'ON CONFLICT (key,bucket_start) DO UPDATE SET count=clickclash.buckets.count+1 '
                       'WHERE clickclash.buckets.count < %s RETURNING count', (key, seconds, seconds, limit)).fetchone()
    return row is not None


def auth_admission(request, kind):
    address = request.client.host if request.client else 'unknown'
    trusted = os.environ.get('CLICKCLASH_TRUSTED_PROXY_NETWORK')
    if trusted:
        try:
            if ipaddress.ip_address(address) in ipaddress.ip_network(trusted):
                # The gateway overwrites this header; never trust arbitrary X-Forwarded-For.
                address = str(ipaddress.ip_address(request.headers.get('x-cc-client-ip', address)))
        except ValueError:
            pass
    peer = digest(address)
    with database.connection() as conn:
        allowed = bucket(conn, 'auth-global', 60, 120) and bucket(conn, kind + ':' + peer, 60, 15)
    if not allowed:
        fail('AUTH_RATE_LIMITED', 429)


def click_admission():
    with database.connection() as conn:
        settings = conn.execute('SELECT * FROM clickclash.control WHERE id').fetchone()
        if settings['paused']:
            fail('GAME_PAUSED', 503)
        allowed = bucket(conn, 'click-global', 1, settings['requests_per_second'])
    if not allowed:
        fail('SERVER_AT_CAPACITY', 503)
    return settings['concurrent_writes']


@asynccontextmanager
async def lifespan(app):
    origin = os.environ.get('CLICKCLASH_ORIGIN', 'http://127.0.0.1:8088')
    if os.environ.get('CLICKCLASH_INSECURE_LOCAL') == '1' and origin not in ('http://127.0.0.1:8088', 'http://localhost:8088'):
        raise RuntimeError('Insecure cookies are permitted only for the documented loopback development origin')
    database.open()
    try:
        with database.connection() as conn:
            conn.execute('SELECT id FROM clickclash.control WHERE id').fetchone()
            conn.execute('SELECT player_id FROM clickclash.image_blobs LIMIT 0')
        yield
    finally:
        database.close()


app = FastAPI(title='ClickClash', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware('http')
async def boundaries(request, call_next):
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        if request.headers.get('origin') != os.environ.get('CLICKCLASH_ORIGIN', 'http://127.0.0.1:8088'):
            return JSONResponse({'detail': {'code': 'ORIGIN_REJECTED'}}, status_code=403)
        limit = 1_048_576 if request.url.path == '/api/profile/image' else 8192
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > limit:
                return JSONResponse({'detail': {'code': 'REQUEST_TOO_LARGE'}}, status_code=413)
        request._body = bytes(body)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self' https://milliyin.github.io; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    if request.url.path.startswith('/api/') and not request.url.path.startswith('/api/images/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.exception_handler(Error)
async def unavailable(request, error):
    return JSONResponse({'detail': {'code': 'DATABASE_UNAVAILABLE'}}, status_code=503, headers={'Retry-After': '1'})


@app.exception_handler(blobs.StorageUnavailable)
async def storage_unavailable(request, error):
    return JSONResponse({'detail': {'code': 'IMAGE_STORAGE_UNAVAILABLE'}}, status_code=503)


@app.exception_handler(RequestValidationError)
async def invalid_request(request, error):
    # FastAPI's default validation payload includes input values (credentials).
    return JSONResponse({'detail': {'code': 'INVALID_REQUEST'}}, status_code=422)


class Signup(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    nickname: str = Field(min_length=2, max_length=32)
    accept_no_recovery: Literal[True]


class Login(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    passphrase: str = Field(min_length=1, max_length=256)


class Profile(BaseModel):
    nickname: str = Field(min_length=2, max_length=32)
    bio: str = Field(default='', max_length=180)
    website: str = Field(default='', max_length=500)


class Click(BaseModel):
    model_config = {'extra': 'forbid'}
    id: UUID


class Report(BaseModel):
    reason: str = Field(min_length=3, max_length=300)


@app.get('/healthz')
def health():
    return {'health': 'alive'}


@app.get('/readyz')
def ready():
    with database.connection() as conn:
        conn.execute('SELECT id FROM clickclash.control WHERE id').fetchone()
    return {'readiness': 'ready'}


@app.post('/api/guest')
def guest(request: Request, response: Response):
    auth_admission(request, 'guest')
    with database.connection() as conn:
        if request.cookies.get(COOKIE):
            existing = conn.execute('SELECT 1 FROM clickclash.sessions WHERE token_hash=%s '
                                    'AND expires_at > clock_timestamp()', (digest(request.cookies[COOKIE]),)).fetchone()
            if existing:
                fail('SESSION_ALREADY_EXISTS', 409)
        player_id = uuid4()
        conn.execute('INSERT INTO clickclash.players(id,nickname) VALUES (%s,%s)',
                     (player_id, 'Guest-' + secrets.token_hex(3)))
        csrf = issue(conn, player_id, response)
    return {'csrf': csrf}


@app.get('/api/me')
def me(request: Request):
    with database.connection() as conn:
        row = session(conn, request)
        row['has_image'] = conn.execute('SELECT 1 FROM clickclash.images WHERE player_id=%s UNION ALL '
                                       'SELECT 1 FROM clickclash.image_blobs WHERE player_id=%s', (row['id'], row['id'])).fetchone() is not None
        return public_player(row) | {'csrf': row['csrf']}


@app.post('/api/signup', status_code=201)
def signup(data: Signup, request: Request, response: Response):
    auth_admission(request, 'signup')
    address = security.email(data.email)
    if len(data.nickname.strip()) < 2:
        fail('INVALID_NICKNAME', 422)
    phrase = security.password()
    encoded = security.hash_password(phrase)
    try:
        with database.connection() as conn:
            row = session(conn, request, write=True)
            player = conn.execute('SELECT * FROM clickclash.players WHERE id=%s FOR UPDATE', (row['id'],)).fetchone()
            session(conn, request, write=True)
            if player['banned'] or player['merged_into']:
                fail('SESSION_EXPIRED', 401)
            if player['email']:
                fail('ALREADY_REGISTERED', 409)
            conn.execute('UPDATE clickclash.players SET email=%s,password_hash=%s,nickname=%s WHERE id=%s',
                         (address, encoded, data.nickname.strip(), row['id']))
            conn.execute('DELETE FROM clickclash.sessions WHERE player_id=%s', (row['id'],))
            csrf = issue(conn, row['id'], response)
    except errors.UniqueViolation:
        fail('SIGNUP_UNAVAILABLE', 409)
    return {'passphrase': phrase, 'csrf': csrf, 'note': 'Shown once. No password recovery is available.'}


@app.post('/api/login')
def login(data: Login, request: Request, response: Response):
    auth_admission(request, 'login')
    address = security.email(data.email)
    with database.connection() as conn:
        guest = session(conn, request, write=True)
        target = conn.execute('SELECT * FROM clickclash.players WHERE email=%s', (address,)).fetchone()
    if not security.verify(target['password_hash'] if target else DUMMY_HASH, data.passphrase) or not target or target['banned']:
        fail('INVALID_CREDENTIALS', 401)
    with database.connection() as conn:
        # Lock in UUID order so parallel guest linking cannot deadlock.
        locked = conn.execute('SELECT * FROM clickclash.players WHERE id=ANY(%s) ORDER BY id FOR UPDATE',
                              ([guest['id'], target['id']],)).fetchall()
        by_id = {p['id']: p for p in locked}
        source, destination = by_id[guest['id']], by_id[target['id']]
        session(conn, request, write=True)
        if (destination['banned'] or source['banned'] or source['merged_into']
                or destination['email'] != address or destination['password_hash'] != target['password_hash']):
            fail('SESSION_EXPIRED', 401)
        if source['id'] != destination['id'] and source['email'] is None:
            conn.execute('UPDATE clickclash.players SET score=score+%s WHERE id=%s', (source['score'], target['id']))
            conn.execute('UPDATE clickclash.receipts SET player_id=%s WHERE player_id=%s', (target['id'], source['id']))
            conn.execute('UPDATE clickclash.players SET score=0,merged_into=%s WHERE id=%s', (target['id'], source['id']))
            conn.execute('DELETE FROM clickclash.sessions WHERE player_id=%s', (source['id'],))
        conn.execute('DELETE FROM clickclash.sessions WHERE token_hash=%s', (digest(request.cookies[COOKIE]),))
        csrf = issue(conn, target['id'], response)
    return {'csrf': csrf}


@app.post('/api/logout')
def logout(request: Request, response: Response):
    with database.connection() as conn:
        session(conn, request, write=True)
        conn.execute('DELETE FROM clickclash.sessions WHERE token_hash=%s', (digest(request.cookies[COOKIE]),))
    response.delete_cookie(COOKIE, path='/')
    return {'logged_out': True}


@app.post('/api/click')
def click(data: Click, request: Request):
    slots = click_admission()
    with database.connection() as conn:
        player = session(conn, request, write=True)
        # Cluster-wide bounded write concurrency, held until commit/rollback.
        start = secrets.randbelow(slots)
        for n in range(slots):
            if conn.execute('SELECT pg_try_advisory_xact_lock(%s) AS acquired',
                            (8432100 + (start + n) % slots,)).fetchone()['acquired']:
                break
        else:
            fail('SERVER_AT_CAPACITY', 503)
        owner = conn.execute('SELECT * FROM clickclash.players WHERE id=%s FOR UPDATE', (player['id'],)).fetchone()
        session(conn, request, write=True)
        if owner['merged_into'] or owner['banned']:
            fail('SESSION_EXPIRED', 401)
        if conn.execute('SELECT paused FROM clickclash.control WHERE id').fetchone()['paused']:
            fail('GAME_PAUSED', 503)
        inserted = conn.execute('INSERT INTO clickclash.receipts(id,player_id) VALUES (%s,%s) '
                                'ON CONFLICT (id) DO NOTHING RETURNING id', (data.id, owner['id'])).fetchone()
        if not inserted:
            receipt = conn.execute('SELECT player_id FROM clickclash.receipts WHERE id=%s', (data.id,)).fetchone()
            if receipt['player_id'] != owner['id']:
                fail('CLICK_ID_CONFLICT', 409)
            score = owner['score']
        else:
            score = conn.execute('UPDATE clickclash.players SET score=score+1 WHERE id=%s RETURNING score',
                                 (owner['id'],)).fetchone()['score']
    return {'id': data.id, 'score': score, 'duplicate': not bool(inserted)}


@app.get('/api/leaderboard')
def leaderboard(offset: int = 0):
    if not 0 <= offset <= 1000:
        fail('INVALID_PAGE', 422)
    with database.connection() as conn:
        rows = conn.execute('SELECT p.id,p.nickname,p.score,p.ad_hidden, '
                            'coalesce((SELECT digest FROM clickclash.image_blobs b WHERE b.player_id=p.id), '
                            '(SELECT digest FROM clickclash.images i WHERE i.player_id=p.id)) AS image_digest, '
                            "CASE WHEN p.ad_hidden THEN '' ELSE p.bio END AS bio, "
                            "CASE WHEN p.ad_hidden THEN '' ELSE p.website END AS website, "
                            '(NOT p.ad_hidden AND (EXISTS(SELECT 1 FROM clickclash.images i WHERE i.player_id=p.id) '
                            'OR EXISTS(SELECT 1 FROM clickclash.image_blobs b WHERE b.player_id=p.id))) AS has_image '
                            'FROM clickclash.players p WHERE email IS NOT NULL AND merged_into IS NULL AND NOT banned '
                            'ORDER BY score DESC,created_at,id LIMIT 25 OFFSET %s', (offset,)).fetchall()
        totals = conn.execute('SELECT count(*) AS players,coalesce(sum(score),0) AS clicks '
                              'FROM clickclash.players WHERE email IS NOT NULL AND merged_into IS NULL AND NOT banned').fetchone()
        paused = conn.execute('SELECT paused FROM clickclash.control WHERE id').fetchone()['paused']
    return {'entries': [board_player(row) for row in rows],
            'totals': totals, 'paused': paused, 'offset': offset}


@app.put('/api/profile')
def profile(data: Profile, request: Request):
    link = security.website(data.website.strip())
    name = data.nickname.strip()
    if len(name) < 2:
        fail('INVALID_NICKNAME', 422)
    with database.connection() as conn:
        player = session(conn, request, write=True)
        conn.execute('SELECT id FROM clickclash.players WHERE id=%s FOR UPDATE', (player['id'],))
        session(conn, request, write=True)
        conn.execute('UPDATE clickclash.players SET nickname=%s,bio=%s,website=%s WHERE id=%s',
                     (name, data.bio.strip(), link, player['id']))
    return {'saved': True}


@app.put('/api/profile/image')
async def upload_image(request: Request):
    # CPU-bound decode is moved off the event loop.
    from starlette.concurrency import run_in_threadpool
    raw = await request.body()
    def save():
        auth_admission(request, 'image')
        with database.connection() as conn:
            player = session(conn, request, write=True)
            if not player['email']:
                fail('ACCOUNT_REQUIRED', 403)
            encoded = security.image(raw)
            # Shared storage budget, so arbitrary upload volume cannot fill the DB.
            conn.execute('SELECT id FROM clickclash.control WHERE id FOR UPDATE')
            # Recheck session after serializing image/account mutations.
            player = session(conn, request, write=True)
            stored = conn.execute('SELECT * FROM clickclash.image_blobs WHERE player_id=%s', (player['id'],)).fetchone()
            previous = conn.execute('SELECT octet_length(content) AS size FROM clickclash.images WHERE player_id=%s', (player['id'],)).fetchone()
            delta = len(encoded) - (stored['byte_size'] if stored else 0) - (previous['size'] if previous else 0)
            updated = conn.execute('UPDATE clickclash.control SET image_bytes=image_bytes+%s '
                                   'WHERE id AND image_bytes+%s <= 104857600 RETURNING id', (delta, delta)).fetchone()
            if not updated:
                fail('IMAGE_STORAGE_FULL', 503)
            if blobs.enabled():
                blob_name, digest_value = blobs.put(player['id'], encoded)
                conn.execute('INSERT INTO clickclash.image_blobs VALUES (%s,%s,%s,%s) ON CONFLICT(player_id) '
                             'DO UPDATE SET blob_name=excluded.blob_name,digest=excluded.digest,byte_size=excluded.byte_size',
                             (player['id'], blob_name, digest_value, len(encoded)))
                conn.execute('DELETE FROM clickclash.images WHERE player_id=%s', (player['id'],))
            else:
                if stored:
                    fail('IMAGE_STORAGE_UNAVAILABLE', 503)  # Never silently downgrade a migrated record.
                conn.execute('INSERT INTO clickclash.images VALUES (%s,%s,%s) ON CONFLICT(player_id) '
                             'DO UPDATE SET content=excluded.content,digest=excluded.digest',
                             (player['id'], encoded, hashlib.sha256(encoded).hexdigest()))
        return {'saved': True}
    if not IMAGE_SLOTS.acquire(blocking=False):
        fail('IMAGE_UPLOAD_BUSY', 503)
    try:
        return await run_in_threadpool(save)
    finally:
        IMAGE_SLOTS.release()


@app.get('/api/images/{player_id}')
def get_image(player_id: UUID):
    with database.connection() as conn:
        stored = conn.execute('SELECT b.* FROM clickclash.image_blobs b JOIN clickclash.players p ON p.id=b.player_id '
                              'WHERE b.player_id=%s AND NOT p.ad_hidden AND NOT p.banned AND p.email IS NOT NULL', (player_id,)).fetchone()
        row = conn.execute('SELECT i.* FROM clickclash.images i JOIN clickclash.players p ON p.id=i.player_id '
                           'WHERE i.player_id=%s AND NOT p.ad_hidden AND NOT p.banned AND p.email IS NOT NULL', (player_id,)).fetchone()
    if stored:
        content = blobs.read(stored['blob_name'], player_id, stored['digest'])
        return Response(content, media_type='image/webp', headers={'Cache-Control': 'no-store'})
    if not row:
        fail('IMAGE_NOT_FOUND', 404)
    return Response(bytes(row['content']), media_type='image/webp',
                    headers={'Cache-Control': 'no-store', 'ETag': '"' + row['digest'] + '"'})


@app.delete('/api/profile/image')
def remove_image(request: Request):
    with database.connection() as conn:
        player = session(conn, request, write=True)
        conn.execute('SELECT id FROM clickclash.control WHERE id FOR UPDATE')
        session(conn, request, write=True)
        stored = conn.execute('DELETE FROM clickclash.image_blobs WHERE player_id=%s RETURNING byte_size', (player['id'],)).fetchone()
        previous = conn.execute('DELETE FROM clickclash.images WHERE player_id=%s RETURNING octet_length(content) AS size', (player['id'],)).fetchone()
        if previous:
            conn.execute('UPDATE clickclash.control SET image_bytes=image_bytes-%s WHERE id', (previous['size'],))
        if stored:
            conn.execute('UPDATE clickclash.control SET image_bytes=image_bytes-%s WHERE id', (stored['byte_size'],))
    return {'removed': True}


@app.delete('/api/account')
def delete_account(request: Request):
    with database.connection() as conn:
        player = session(conn, request, write=True)
        conn.execute('SELECT id FROM clickclash.control WHERE id FOR UPDATE')
        session(conn, request, write=True)
        stored = conn.execute('DELETE FROM clickclash.image_blobs WHERE player_id=%s RETURNING byte_size', (player['id'],)).fetchone()
        conn.execute('SELECT id FROM clickclash.players WHERE id=%s FOR UPDATE', (player['id'],))
        removed = conn.execute('DELETE FROM clickclash.images WHERE player_id=%s RETURNING octet_length(content) AS size', (player['id'],)).fetchone()
        if removed:
            conn.execute('UPDATE clickclash.control SET image_bytes=image_bytes-%s WHERE id', (removed['size'],))
        if stored:
            conn.execute('UPDATE clickclash.control SET image_bytes=image_bytes-%s WHERE id', (stored['byte_size'],))
        conn.execute("UPDATE clickclash.players SET email=NULL,password_hash=NULL,nickname='Deleted player',"
                     "bio='',website='',score=0,banned=true WHERE id=%s", (player['id'],))
        conn.execute('DELETE FROM clickclash.reports WHERE reporter=%s OR target=%s', (player['id'], player['id']))
        conn.execute('DELETE FROM clickclash.sessions WHERE player_id=%s', (player['id'],))
    response = JSONResponse({'deleted': True})
    response.delete_cookie(COOKIE, path='/')
    return response


@app.post('/api/report/{player_id}')
def report(player_id: UUID, data: Report, request: Request):
    auth_admission(request, 'report')
    with database.connection() as conn:
        player = session(conn, request, write=True)
        if not conn.execute('SELECT 1 FROM clickclash.players WHERE id=%s AND email IS NOT NULL', (player_id,)).fetchone():
            fail('PLAYER_NOT_FOUND', 404)
        conn.execute('INSERT INTO clickclash.reports(reporter,target,reason) VALUES (%s,%s,%s) '
                     'ON CONFLICT(reporter,target) DO UPDATE SET reason=excluded.reason', (player['id'], player_id, data.reason))
    return {'reported': True}


if (DIST / 'assets').is_dir():
    app.mount('/assets', StaticFiles(directory=DIST / 'assets'), name='assets')


@app.get('/')
@app.get('/play')
@app.get('/account')
@app.get('/status')
def frontend():
    if not (DIST / 'index.html').is_file():
        fail('FRONTEND_BUILD_REQUIRED', 503)
    return FileResponse(DIST / 'index.html', headers={'Cache-Control': 'no-cache'})
