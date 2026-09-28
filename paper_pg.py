"""PostgreSQL adapter for persistent paper episodes (no exchange credentials)."""

import psycopg
from psycopg.rows import dict_row
import threading

_schema_lock = threading.Lock()
_schema_ready = False


SCHEMA = '''
CREATE TABLE IF NOT EXISTS episodes (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    spot_exchange TEXT NOT NULL,
    futures_exchange TEXT NOT NULL,
    direction TEXT NOT NULL,
    started_at DOUBLE PRECISION NOT NULL,
    target_usdt TEXT NOT NULL,
    actual_spot_usdt TEXT NOT NULL,
    actual_futures_usdt TEXT NOT NULL,
    quantity TEXT NOT NULL,
    spot_ask TEXT NOT NULL,
    spot_vwap TEXT NOT NULL,
    futures_bid TEXT NOT NULL,
    futures_vwap TEXT NOT NULL,
    raw_spread_pct TEXT NOT NULL,
    executable_spread_pct TEXT NOT NULL,
    net_projected_pct TEXT NOT NULL,
    funding_rate TEXT NOT NULL,
    next_funding_at DOUBLE PRECISION NOT NULL,
    funding_cost_usdt TEXT NOT NULL,
    cost_snapshot_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'observing',
    first_close_min INTEGER,
    min_spread_pct TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS episode_lookup ON episodes
    (symbol, spot_exchange, futures_exchange, started_at DESC);
CREATE TABLE IF NOT EXISTS checkpoints (
    episode_id BIGINT NOT NULL REFERENCES episodes(id),
    horizon_min INTEGER NOT NULL,
    due_at DOUBLE PRECISION NOT NULL,
    sampled_at DOUBLE PRECISION,
    status TEXT NOT NULL,
    spot_ask TEXT,
    spot_vwap TEXT,
    futures_bid TEXT,
    futures_vwap TEXT,
    raw_spread_pct TEXT,
    executable_spread_pct TEXT,
    reduction_pp TEXT,
    reduction_pct TEXT,
    reason TEXT,
    PRIMARY KEY (episode_id, horizon_min)
);
CREATE INDEX IF NOT EXISTS checkpoint_due ON checkpoints(status, due_at);
'''


class PgConnection:
    is_postgres = True

    def __init__(self, url):
        global _schema_ready
        self.connection = psycopg.connect(url, connect_timeout=6, row_factory=dict_row)
        try:
            with _schema_lock:
                if not _schema_ready:
                    with self.connection.transaction():
                        for statement in SCHEMA.split(';'):
                            if statement.strip():
                                self.connection.execute(statement)
                    _schema_ready = True
        except Exception:
            self.connection.close()
            raise

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, *args):
        return self.connection.__exit__(*args)

    def execute(self, sql, params=()):
        sql = sql.replace('?', '%s').replace('AS REAL', 'AS DOUBLE PRECISION')
        return self.connection.execute(sql, params)

    def executemany(self, sql, rows):
        with self.connection.cursor() as cursor:
            cursor.executemany(sql.replace('?', '%s'), rows)

    def close(self):
        self.connection.close()
