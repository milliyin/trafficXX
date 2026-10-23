from fastapi import HTTPException
from psycopg import Error

from trafficxx.db import connect


def register(app, readiness_query='SELECT 1 FROM events LIMIT 1', connection_factory=connect):
    @app.get('/healthz')
    def health():
        return {'status': 'alive'}

    @app.get('/readyz')
    def ready():
        try:
            with connection_factory() as conn:
                conn.execute(readiness_query)
        except Error:
            raise HTTPException(503, detail={'code': 'DATABASE_UNAVAILABLE'}) from None
        return {'status': 'ready'}
