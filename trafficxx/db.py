import os

import psycopg
from psycopg.rows import dict_row


def connect():
    return psycopg.connect(
        os.environ['DATABASE_URL'], row_factory=dict_row, connect_timeout=5,
        options='-c statement_timeout=15000 -c lock_timeout=10000',
    )
