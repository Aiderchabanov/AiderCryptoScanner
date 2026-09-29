"""Persistent, read-only observations of spot/perpetual spread episodes.

Production uses a TLS-protected PostgreSQL connection. Local SQLite files
remain available for isolated tests. No exchange order or transfer API is used.
"""

import json
import logging
import os
import sqlite3
import statistics
import time
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HORIZONS = (1, 5, 15, 30, 60)
WINDOW_SECONDS = 60
EPISODE_COOLDOWN_SECONDS = 7200
MIN_HISTORY = 30
CLOSED_PCT = Decimal('0.10')
_storage_check = (0, False)


def database_url():
    value = os.getenv('PAPER_DATABASE_URL', '')
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in ('postgres', 'postgresql') or not parsed.hostname:
        raise RuntimeError('Invalid PostgreSQL configuration')
    if parse_qs(parsed.query).get('sslmode', [''])[0] not in ('require', 'verify-ca', 'verify-full'):
        raise RuntimeError('PostgreSQL requires TLS (sslmode=require)')
    return value


def configured_path():
    value = os.getenv('PAPER_DB_PATH', '')
    if not value:
        return None
    path = Path(value).resolve()
    # Render's free service filesystem is ephemeral. Fail closed if the path
    # does not point at the explicitly mounted persistent disk.
    if os.getenv('RENDER') and not path.is_relative_to(Path('/var/data')):
        raise RuntimeError('PAPER_DB_PATH must be on the /var/data persistent disk')
    if path.is_relative_to(Path('/var/data')) and not os.path.ismount('/var/data'):
        raise RuntimeError('/var/data is not a mounted persistent disk')
    return path


def storage_ready():
    global _storage_check
    try:
        if database_url():
            now = time.monotonic()
            if now < _storage_check[0]:
                return _storage_check[1]
            with session() as db:
                db.execute('SELECT 1')
            _storage_check = (now + 30, True)
            return True
        return configured_path() is not None
    except Exception as exc:
        logging.warning('Paper database unavailable: %s', type(exc).__name__)
        _storage_check = (time.monotonic() + 15, False)
        return False


def connect(path=None):
    if path is None and database_url():
        try:
            from paper_pg import PgConnection
            return PgConnection(database_url())
        except Exception:
            raise RuntimeError('PostgreSQL observation storage unavailable') from None
    path = Path(path) if path else configured_path()
    if path is None:
        raise RuntimeError('Persistent observation storage is not configured')
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=10000')
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS episodes (
            id INTEGER PRIMARY KEY,
            symbol TEXT NOT NULL,
            spot_exchange TEXT NOT NULL,
            futures_exchange TEXT NOT NULL,
            direction TEXT NOT NULL,
            started_at REAL NOT NULL,
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
            next_funding_at REAL NOT NULL,
            funding_cost_usdt TEXT NOT NULL,
            cost_snapshot_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'observing',
            first_close_min INTEGER,
            min_spread_pct TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS episode_lookup ON episodes
          (symbol, spot_exchange, futures_exchange, started_at DESC);
        CREATE TABLE IF NOT EXISTS checkpoints (
            episode_id INTEGER NOT NULL REFERENCES episodes(id),
            horizon_min INTEGER NOT NULL,
            due_at REAL NOT NULL,
            sampled_at REAL,
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
    ''')
    return db


@contextmanager
def session(path=None):
    db = connect(path)
    try:
        with db:
            yield db
    finally:
        db.close()


def first_value(row):
    return next(iter(row.values())) if isinstance(row, dict) else row[0]


def load_api_backoff(host):
    """Read a host ban deadline across deploys, without storing credentials."""
    with session() as db:
        row = db.execute('SELECT until_at FROM api_backoff WHERE host=?', (host,)).fetchone()
        return float(first_value(row)) if row else 0


def save_api_backoff(host, until_at):
    with session() as db:
        db.execute('''INSERT INTO api_backoff (host, until_at) VALUES (?, ?)
            ON CONFLICT (host) DO UPDATE SET until_at=excluded.until_at
            WHERE api_backoff.until_at < excluded.until_at''', (host, until_at))





def record(item, path=None, at=None):
    """Create one episode for a qualified alert; return its ID or None."""
    now = time.time() if at is None else at
    try:
        rate = Decimal(str(item['funding']))
        next_at = float(item['next_funding_at'])
    except (KeyError, InvalidOperation, ValueError, TypeError):
        return None
    if not rate.is_finite() or rate <= 0 or next_at <= now:
        return None  # Defense in depth; basis.scan also rechecks live funding.
    with session(path) as db:
        if getattr(db, 'is_postgres', False):
            # Serialize episode creation for this pair, including the first row.
            db.execute('SELECT pg_advisory_xact_lock(hashtext(?))',
                       (item['symbol'] + ':' + item['spot'] + ':' + item['future'],))
        else:
            db.execute('BEGIN IMMEDIATE')
        prior = db.execute('''SELECT * FROM episodes WHERE symbol=? AND spot_exchange=?
               AND futures_exchange=? ORDER BY started_at DESC LIMIT 1''',
               (item['symbol'], item['spot'], item['future'])).fetchone()
        if prior and (prior['status'] == 'observing' or
                      (prior['first_close_min'] is None and
                       now < prior['started_at'] + EPISODE_COOLDOWN_SECONDS)):
            return None
        raw = (item['future_bid'] / item['spot_ask'] - 1) * 100
        executable = (item['future_entry'] / item['spot_entry'] - 1) * 100
        # Explicit allowlist: never persist API credentials or future private fields.
        saved_fields = ('symbol', 'spot', 'future', 'quantity', 'spot_cost',
                        'future_notional', 'spot_ask', 'spot_entry', 'future_bid',
                        'future_entry', 'pct', 'funding', 'next_funding_at',
                        'funding_debit', 'spot_fee', 'future_fee',
                        'spot_slippage_usdt', 'futures_slippage_usdt',
                        'price_buffer', 'multiplier')
        snapshot = {key: str(item[key]) for key in saved_fields if key in item}
        row = (item['symbol'], item['spot'], item['future'], 'spot_buy/futures_short',
               now, '50', str(item['spot_cost']), str(item['future_notional']),
               str(item['quantity']), str(item['spot_ask']), str(item['spot_entry']),
               str(item['future_bid']), str(item['future_entry']), str(raw),
               str(executable), str(item['pct']), str(item['funding']),
               item['next_funding_at'], str(item['funding_debit']),
               json.dumps(snapshot, ensure_ascii=False), str(executable))
        cursor = db.execute('''INSERT INTO episodes
            (symbol, spot_exchange, futures_exchange, direction, started_at,
             target_usdt, actual_spot_usdt, actual_futures_usdt, quantity,
             spot_ask, spot_vwap, futures_bid, futures_vwap, raw_spread_pct,
             executable_spread_pct, net_projected_pct, funding_rate,
             next_funding_at, funding_cost_usdt, cost_snapshot_json, min_spread_pct)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)'''
            + (' RETURNING id' if getattr(db, 'is_postgres', False) else ''), row)
        episode_id = cursor.fetchone()['id'] if getattr(db, 'is_postgres', False) else cursor.lastrowid
        db.executemany('''INSERT INTO checkpoints
            (episode_id, horizon_min, due_at, status) VALUES (?,?,?,?)''',
            [(episode_id, minute, now + minute * 60, 'pending') for minute in HORIZONS])
        return episode_id


def executable_sample(api, episode):
    """Fresh matched-quantity ask/bid books; never use last-trade prices."""
    symbol = episode['symbol']
    qty = Decimal(episode['quantity'])
    original = json.loads(episode['cost_snapshot_json'])
    multiplier = api.dec(original.get('multiplier'))
    if not multiplier or multiplier <= 0:
        raise ValueError('Original futures contract multiplier unavailable')
    asks, _ = api.orderbook(episode['spot_exchange'], symbol)
    _, bids = api.basis.futures_book(api, episode['futures_exchange'], symbol,
                                     multiplier)
    if not asks or not bids:
        raise ValueError('orderbook unavailable')
    spot_ask = api.dec(asks[0][0])
    future_bid = api.dec(bids[0][0])
    spent = api.basis.spot_cost(asks, qty, api)
    received = api.sell_for_usdt(bids, qty)
    if not spot_ask or not future_bid or spent is None or received is None:
        raise ValueError('insufficient or malformed orderbook')
    spot_vwap, future_vwap = spent / qty, received / qty
    return {'spot_ask': spot_ask, 'spot_vwap': spot_vwap,
            'futures_bid': future_bid, 'futures_vwap': future_vwap,
            'raw_spread_pct': (future_bid / spot_ask - 1) * 100,
            'executable_spread_pct': (future_vwap / spot_vwap - 1) * 100}


def finish_checkpoint(db, episode, minute, now, sample=None, reason=None):
    first = Decimal(episode['executable_spread_pct'])
    if sample is None:
        db.execute('''UPDATE checkpoints SET status='missing', sampled_at=?, reason=?
                      WHERE episode_id=? AND horizon_min=? AND status='pending' ''',
                   (now, reason or 'нет данных', episode['id'], minute))
    else:
        spread = sample['executable_spread_pct']
        reduction = first - spread
        relative = reduction / first * 100 if first > 0 else None
        db.execute('''UPDATE checkpoints SET status='observed', sampled_at=?,
                spot_ask=?, spot_vwap=?, futures_bid=?, futures_vwap=?,
                raw_spread_pct=?, executable_spread_pct=?, reduction_pp=?,
                reduction_pct=? WHERE episode_id=? AND horizon_min=? AND status='pending' ''',
                   (now, *(str(sample[key]) for key in
                     ('spot_ask', 'spot_vwap', 'futures_bid', 'futures_vwap',
                      'raw_spread_pct', 'executable_spread_pct')),
                    str(reduction), str(relative) if relative is not None else None,
                    episode['id'], minute))
        db.execute('''UPDATE episodes SET min_spread_pct=? WHERE id=?
                      AND CAST(min_spread_pct AS REAL)>?''',
                   (str(spread), episode['id'], float(spread)))
        if spread <= CLOSED_PCT:
            db.execute('''UPDATE episodes SET first_close_min=COALESCE(first_close_min,?),
                          status='closed' WHERE id=?''', (minute, episode['id']))
    if minute == 60:
        pending = first_value(db.execute("SELECT COUNT(*) FROM checkpoints WHERE episode_id=? AND status!='observed'",
                                         (episode['id'],)).fetchone())
        if pending and not db.execute('SELECT first_close_min FROM episodes WHERE id=?',
                                     (episode['id'],)).fetchone()['first_close_min']:
            state = 'incomplete'
        else:
            state = 'closed' if db.execute('SELECT first_close_min FROM episodes WHERE id=?',
                                           (episode['id'],)).fetchone()['first_close_min'] else 'not_closed_60m'
        db.execute('UPDATE episodes SET status=? WHERE id=?', (state, episode['id']))


def poll(api, path=None, at=None):
    now = time.time() if at is None else at
    with session(path) as db:
        sql = '''SELECT e.*, c.horizon_min, c.due_at FROM checkpoints c
            JOIN episodes e ON e.id=c.episode_id
            WHERE c.status='pending' AND c.due_at<=? ORDER BY c.due_at LIMIT 30'''
        if getattr(db, 'is_postgres', False):
            sql += ' FOR UPDATE OF c SKIP LOCKED'
        rows = db.execute(sql, (now,)).fetchall()
        for episode in rows:
            minute = episode['horizon_min']
            if now > episode['due_at'] + WINDOW_SECONDS:
                finish_checkpoint(db, episode, minute, now, reason='нет данных: окно замера пропущено')
                continue
            try:
                sample = executable_sample(api, episode)
            except Exception as exc:
                logging.warning('Virtual checkpoint %s +%sm unavailable: %s',
                                episode['symbol'], minute, type(exc).__name__)
                continue  # Retry until the sampling window expires.
            finish_checkpoint(db, episode, minute, now, sample=sample)


def statistics_for(db, symbol=None):
    query = 'SELECT e.id, e.first_close_min, e.status FROM episodes e'
    params = ()
    if symbol:
        query += ' WHERE e.symbol=?'
        params = (symbol,)
    rows = db.execute(query, params).fetchall()
    complete = []
    for row in rows:
        samples = first_value(db.execute("SELECT COUNT(*) FROM checkpoints WHERE episode_id=? AND status='observed'",
                                         (row['id'],)).fetchone())
        if samples == len(HORIZONS):
            complete.append(row)
    times = [x['first_close_min'] for x in complete if x['first_close_min'] is not None]
    n = len(complete)
    return {'observations': len(rows), 'complete': n, 'incomplete': len(rows) - n,
            'closed_by_pct': {str(t): round(100 * sum(x <= t for x in times) / n, 1)
                              if n else None for t in HORIZONS},
            'median_close_min': statistics.median(times) if times else None,
            'mean_close_min': round(statistics.mean(times), 2) if times else None,
            'not_closed_60m_pct': round(100 * (n - len(times)) / n, 1) if n else None}


def history_line(path, symbol):
    with session(path) as db:
        data = statistics_for(db, symbol)
    n = data['complete']
    if n < MIN_HISTORY:
        return f"История {symbol[:-4]}: {n} полных наблюдений; недостаточно истории."
    return (f"История {symbol[:-4]}: {n} наблюдений; "
            f"{data['closed_by_pct']['15']}% закрылись ≤0,10% к 15 мин; "
            f"медиана первого наблюдения закрытия {data['median_close_min']} мин.")


def stats_line(path=None, symbol=None):
    with session(path) as db:
        data = statistics_for(db, symbol)
    label = symbol[:-4] if symbol else 'весь сканер'
    rates = data['closed_by_pct']
    pct = lambda value: 'нет данных' if value is None else f'{value}%'
    return (f"История {label}: {data['observations']} эпизодов, "
            f"{data['complete']} полных наблюдений; "
            f"неполных {data['incomplete']}.\n"
            + ('Недостаточно истории.\n' if data['complete'] < MIN_HISTORY else '')
            + 'Закрылись ≤0,10% к 1/5/15/30/60 мин: '
            + ' / '.join(pct(rates[str(t)]) for t in HORIZONS) + '.\n'
            + f"Медиана первого наблюдения закрытия: {data['median_close_min']} мин; "
            + f"среднее: {data['mean_close_min']} мин. "
            + f"Не закрылись за 60 мин: {pct(data['not_closed_60m_pct'])}.")
