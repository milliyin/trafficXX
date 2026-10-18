from fastapi import HTTPException
from psycopg import Error

from trafficxx.db import connect


def register(app):
    @app.get('/healthz')
    def health():
        return {'status': 'alive'}

    @app.get('/readyz')
    def ready():
        try:
            with connect() as conn:
                conn.execute('SELECT 1 FROM events LIMIT 1')
        except Error:
            raise HTTPException(503, detail={'code': 'DATABASE_UNAVAILABLE'}) from None
        return {'status': 'ready'}
