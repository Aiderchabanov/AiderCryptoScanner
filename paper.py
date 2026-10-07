"""Persistent, read-only observations of spot/perpetual spread episodes.

Production uses a TLS-protected PostgreSQL connection. Local SQLite files
remain available for isolated tests. No exchange order or transfer API is used.
"""

import json
import bingx_diagnostics as bxdiag
import logging
import math
import os
import sqlite3
import statistics
import time
from contextlib import contextmanager, nullcontext
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HORIZONS = (1, 5, 15, 30, 60)
EXTENDED_HORIZONS = (180, 360, 720, 1440)
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
        until = float(first_value(row)) if row else 0
        if host in ('api.binance.com', 'fapi.binance.com'):
            # Read existing Monitor bans too, including Spot bans saved before
            # the shared GET gate was introduced. No schema changes.
            exists = db.execute("SELECT to_regclass('risk_hosts')") if getattr(db, 'is_postgres', False) else db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='risk_hosts'")
            table = exists.fetchone()
            if table and first_value(table):
                risk = db.execute('SELECT until_at FROM risk_hosts WHERE host=?', (host,)).fetchone()
                if risk:
                    until = max(until, float(first_value(risk)))
        return until


def save_api_backoff(host, until_at):
    with session() as db:
        db.execute('''INSERT INTO api_backoff (host, until_at) VALUES (?, ?)
            ON CONFLICT (host) DO UPDATE SET until_at=excluded.until_at
            WHERE api_backoff.until_at < excluded.until_at''', (host, until_at))





def record(item, path=None, at=None, parent_id=None, connection=None):
    import entry_diagnostics
    try:
        ident=_record(item,path,at,parent_id,connection)
    except Exception as exc:
        database_error=isinstance(exc,sqlite3.Error) or type(exc).__module__.split('.')[0] in ('psycopg','psycopg2')
        entry_diagnostics.flow('PAPER_RECORD_EXCEPTION',item,reason='PERSISTENCE_FAILURE' if database_error else 'RECORD_EXCEPTION',exception_type=type(exc).__name__)
        raise
    if ident is not None:entry_diagnostics.flow('PAPER_RECORD_CREATED',item,episode_id=ident)
    return ident


def _record(item, path=None, at=None, parent_id=None, connection=None):
    """Create one episode for a qualified alert; return its ID or None."""
    import entry_diagnostics
    now = time.time() if at is None else at
    import entry_policy
    item=dict(item)
    item['entry_policy']=entry_policy.VERSION
    rejection=entry_policy.reason(item,now)
    if rejection:
        entry_policy.reject(rejection,item,path,connection)
        entry_diagnostics.flow('PAPER_RECORD_BLOCKED',item,reason='ENTRY_POLICY_REJECTED')
        return None
    try:
        rate = Decimal(str(item['funding']))
        next_at = float(item['next_funding_at'])
    except (KeyError, InvalidOperation, ValueError, TypeError):
        entry_diagnostics.flow('PAPER_RECORD_BLOCKED',item,reason='FUNDING_PARSE_FAILED')
        return None
    if not rate.is_finite() or rate <= 0 or not math.isfinite(next_at) or next_at <= now:
        entry_diagnostics.flow('PAPER_RECORD_BLOCKED',item,reason='FUNDING_INVALID_OR_NONPOSITIVE')
        return None  # Defense in depth; basis.scan also rechecks live funding.
    if 'verified_at' in item and (not math.isfinite(item['verified_at']) or not 0 <= now-item['verified_at'] <= 30):
        entry_diagnostics.flow('PAPER_RECORD_BLOCKED',item,reason='VERIFIED_AT_STALE')
        return None
    raw = (item['future_bid'] / item['spot_ask'] - 1) * 100
    executable = (item['future_entry'] / item['spot_entry'] - 1) * 100
    if raw < 0 or executable < 0:
        if not (Decimal('-2') <= raw < 0 and Decimal('-2') <= executable < 0
                and next_at - now >= 4 * 3600):
            entry_diagnostics.flow('PAPER_RECORD_BLOCKED',item,reason='NEGATIVE_SPREAD_RULE_FAILED')
            return None
    elif raw == 0 or executable == 0:
        entry_diagnostics.flow('PAPER_RECORD_BLOCKED',item,reason='ZERO_SPREAD')
        return None
    import virtual
    with (nullcontext(connection) if connection is not None else session(path)) as db:
        virtual.ensure(db)
        virtual.lock(db)
        if getattr(db, 'is_postgres', False):
            # Serialize episode creation for this pair, including the first row.
            db.execute('SELECT pg_advisory_xact_lock(hashtext(?))',
                       (item['symbol'] + ':' + item['spot'] + ':' + item['future'],))
        elif connection is None:
            db.execute('BEGIN IMMEDIATE')
        if parent_id is None and db.execute('''SELECT 1 FROM episodes e JOIN virtual_state v ON e.id=v.episode_id
                WHERE e.symbol=? AND e.spot_exchange=? AND e.futures_exchange=? AND v.state='open' ''',
                (item['symbol'], item['spot'], item['future'])).fetchone():
            entry_diagnostics.flow('PAPER_RECORD_BLOCKED',item,reason='EXISTING_OPEN_EPISODE')
            return None
        prior = db.execute('''SELECT * FROM episodes WHERE symbol=? AND spot_exchange=?
               AND futures_exchange=? ORDER BY started_at DESC LIMIT 1''',
               (item['symbol'], item['spot'], item['future'])).fetchone()
        if parent_id is None and prior:
            state = db.execute('SELECT * FROM virtual_state WHERE episode_id=?', (prior['id'],)).fetchone()
            if state and state['state']=='closed' and not db.execute('SELECT 1 FROM virtual_meta WHERE name=?', (f"gapreset:{prior['id']}",)).fetchone():
                entry_diagnostics.flow('PAPER_RECORD_BLOCKED',item,reason='PRIOR_CLOSED_WITHOUT_GAPRESET')
                return None
        if parent_id is None and prior and (prior['status'] == 'observing' or
                      (prior['first_close_min'] is None and
                       now < prior['started_at'] + EPISODE_COOLDOWN_SECONDS)):
            entry_diagnostics.flow('PAPER_RECORD_BLOCKED',item,reason='CONTINUOUS_EPISODE_OR_COOLDOWN')
            return None
        budget = item.get('paper_budget_usdt')
        if budget is not None:
            budget = Decimal(str(budget))
            if not budget.is_finite() or budget != __import__('basis').LEG_USDT:
                entry_diagnostics.flow('PAPER_RECORD_BLOCKED',item,reason='PAPER_BUDGET_INVALID')
                return None
            import exchange_capital
            if not exchange_capital.admits(db, item):
                entry_policy.reject('REJECTED_MAX_DEPOSIT_LOAD',item,path,db)
                entry_diagnostics.flow('PAPER_RECORD_BLOCKED',item,reason='MAX_WORKING_CAPITAL')
                return None
            for key in ('spot_fee', 'future_fee', 'multiplier'):
                value = virtual.number(item[key], positive=(key == 'multiplier'))
                if key != 'multiplier' and not Decimal(0) <= value < Decimal(1):
                    entry_diagnostics.flow('PAPER_RECORD_BLOCKED',item,reason='INVALID_FEE_OR_MULTIPLIER')
                    return None
            if parent_id is not None:
                parent = db.execute("SELECT e.symbol,e.spot_exchange,e.futures_exchange FROM episodes e JOIN virtual_state v ON e.id=v.episode_id WHERE e.id=? AND v.state='open'", (parent_id,)).fetchone()
                if not parent or (parent['symbol'], parent['spot_exchange'], parent['futures_exchange']) != (item['symbol'], item['spot'], item['future']):
                    entry_diagnostics.flow('PAPER_RECORD_BLOCKED',item,reason='PARENT_EPISODE_INVALID')
                    return None
        raw = (item['future_bid'] / item['spot_ask'] - 1) * 100
        executable = (item['future_entry'] / item['spot_entry'] - 1) * 100
        # Explicit allowlist: never persist API credentials or future private fields.
        saved_fields = ('symbol', 'spot', 'future', 'quantity', 'spot_cost',
                        'future_notional', 'spot_ask', 'spot_entry', 'future_bid',
                        'future_entry', 'pct', 'funding', 'next_funding_at',
                        'funding_debit', 'spot_fee', 'future_fee',
                        'spot_slippage_usdt', 'futures_slippage_usdt',
                        'price_buffer', 'multiplier', 'paper_budget_usdt', 'entry_policy')
        snapshot = {key: str(item[key]) for key in saved_fields if key in item}
        row = (item['symbol'], item['spot'], item['future'], 'spot_buy/futures_short',
               now, str(budget if budget is not None else item.get('paper_budget_usdt','50')), str(item['spot_cost']), str(item['future_notional']),
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
            [(episode_id, minute, now + minute * 60, 'pending') for minute in HORIZONS + (EXTENDED_HORIZONS if budget is not None else ())])
        if budget is not None:
            virtual.register(db, episode_id, item, parent_id)
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


def lifecycle_sample(api, episode):
    """One fresh pair of books supplies spread and executable closing P&L."""
    import virtual
    q=virtual.quote(api,dict(episode))
    q['spread_change_pp']=str(Decimal(q['spread'])-Decimal(episode['executable_spread_pct']))
    q['spread_reduction_pp']=str(-Decimal(q['spread_change_pp']))
    # A funding quote is not proof of an accrued settlement payment.
    q['funding_rate_current']=None; q['next_funding_at_current']=None
    try:
        rate,next_at=api.basis.fresh_funding(api,episode['futures_exchange'],episode['symbol'])
        rate=virtual.number(rate)
        if next_at is not None and float(next_at)>time.time():
            q['funding_rate_current']=str(rate);q['next_funding_at_current']=float(next_at)
    except Exception:
        pass
    if time.time()-q['at']>30:
        raise ValueError('Checkpoint quotes became stale during funding request')
    return {'spot_ask':Decimal(q['spot_ask']),'spot_vwap':Decimal(q['spot_entry_vwap_now']),
            'futures_bid':Decimal(q['futures_bid']),'futures_vwap':Decimal(q['future_entry_vwap_now']),
            'raw_spread_pct':Decimal(q['raw_spread']),'executable_spread_pct':Decimal(q['spread']),
            'pnl_snapshot':q}


def finish_checkpoint(db, episode, minute, now, sample=None, reason=None):
    first = Decimal(episode['executable_spread_pct'])
    if sample is None:
        db.execute('''UPDATE checkpoints SET status='missing', sampled_at=?, reason=?
                      WHERE episode_id=? AND horizon_min=? AND status='pending' ''',
                   (now, reason or 'нет данных', episode['id'], minute))
    else:
        if sample.get('pnl_snapshot') is not None:
            db.execute('''INSERT INTO virtual_checkpoint_details
                (episode_id,horizon_min,sampled_at,snapshot_json) VALUES (?,?,?,?)
                ON CONFLICT (episode_id,horizon_min) DO NOTHING''',
                (episode['id'],minute,now,json.dumps(sample['pnl_snapshot'])))
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
        import virtual
        virtual.ensure(db)
        managed = db.execute('SELECT state FROM virtual_state WHERE episode_id=?', (episode['id'],)).fetchone()
        if not managed and ((first < 0 and spread >= 0) or (first > 0 and spread <= CLOSED_PCT)):
            db.execute('''UPDATE episodes SET first_close_min=COALESCE(first_close_min,?),
                          status='closed' WHERE id=?''', (minute, episode['id']))
    import virtual
    virtual.ensure(db)
    managed = db.execute('SELECT state FROM virtual_state WHERE episode_id=?', (episode['id'],)).fetchone()
    if minute == 60 and not managed:
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
        import virtual
        virtual.ensure(db)
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
                managed = db.execute('SELECT state FROM virtual_state WHERE episode_id=?',(episode['id'],)).fetchone()
                sample = lifecycle_sample(api, episode) if managed else executable_sample(api, episode)
                if managed:
                    sample['pnl_snapshot']['episode_state']=managed['state']
                    sample['pnl_snapshot']['valuation_kind']='open_position' if managed['state']=='open' else 'hypothetical_after_close'
                actual_now = time.time() if at is None else now
                if actual_now > episode['due_at'] + WINDOW_SECONDS:
                    finish_checkpoint(db,episode,minute,actual_now,reason='нет данных: свежий ответ вне окна замера')
                    continue
                now_sample = actual_now
            except Exception as exc:
                if bxdiag.is_offline(exc):
                    db.execute('SAVEPOINT bingx_offline_checkpoint')
                    try:
                        virtual.record_bingx_offline(db,episode['id'],exc,'checkpoint')
                    except Exception as diagnostic_error:
                        db.execute('ROLLBACK TO SAVEPOINT bingx_offline_checkpoint')
                        logging.warning('Offline state persistence unavailable (%s)',type(diagnostic_error).__name__)
                    finally:
                        db.execute('RELEASE SAVEPOINT bingx_offline_checkpoint')
                bxdiag.emit(exc,episode['id'],episode['symbol'],'checkpoint')
                logging.warning('Virtual checkpoint %s +%sm unavailable: %s',
                                episode['symbol'], minute, type(exc).__name__)
                continue  # Retry until the sampling window expires.
            finish_checkpoint(db, episode, minute, now_sample, sample=sample)
            if managed:
                logging.info('Virtual checkpoint stored: episode #%s +%sm; fresh executable exit/P&L snapshot',episode['id'],minute)


def statistics_for(db, symbol=None, category=None):
    query = "SELECT e.* FROM episodes e WHERE e.direction='spot_buy/futures_short'"
    params = ()
    if category == 'POSITIVE':
        query += ' AND CAST(e.executable_spread_pct AS REAL)>0'
    elif category == 'NEGATIVE':
        query += ' AND CAST(e.executable_spread_pct AS REAL)<0'
    if symbol:
        query += ' AND e.symbol=?'
        params = (symbol,)
    rows = db.execute(query, params).fetchall()
    complete = []
    for row in rows:
        samples = first_value(db.execute("SELECT COUNT(*) FROM checkpoints WHERE episode_id=? AND horizon_min<=60 AND status='observed'",
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


def lifecycle_statistics(db, symbol=None, category=None, at=None):
    """Cohorts use actual convergence, never manual close or fabricated history."""
    import virtual
    virtual.ensure(db)
    now=time.time() if at is None else at
    query="""SELECT e.*,v.state,m.tracking_started_at,m.convergence_seconds
        FROM episodes e JOIN virtual_state v ON v.episode_id=e.id
        JOIN virtual_metrics m ON m.episode_id=e.id
        WHERE e.direction='spot_buy/futures_short' """
    args=[]
    if symbol:query+=' AND e.symbol=?';args.append(symbol)
    if category=='POSITIVE':query+=' AND CAST(e.executable_spread_pct AS REAL)>0'
    if category=='NEGATIVE':query+=' AND CAST(e.executable_spread_pct AS REAL)<0'
    rows=db.execute(query,args).fetchall()
    # Legacy adoption cannot supply unseen entry-to-adoption observations.
    rows=[r for r in rows if r['tracking_started_at']<=r['started_at']+30]
    horizons=HORIZONS+EXTENDED_HORIZONS
    rates={};denominators={}
    for minute in horizons:
        eligible=[]
        for r in rows:
            elapsed=now-r['started_at'];convergence=r['convergence_seconds']
            if elapsed<minute*60:
                continue
            if convergence is not None and convergence<=minute*60:
                eligible.append(True)
            elif elapsed>=minute*60 and (r['state']=='open' or convergence is not None):
                count=first_value(db.execute("SELECT COUNT(*) FROM virtual_checkpoint_details WHERE episode_id=? AND horizon_min<=?",(r['id'],minute)).fetchone())
                if count==sum(t<=minute for t in horizons):eligible.append(False)
        denominators[str(minute)]=len(eligible)
        rates[str(minute)]=round(100*sum(eligible)/len(eligible),1) if eligible else None
    times=[r['convergence_seconds']/60 for r in rows if r['convergence_seconds'] is not None]
    return {'tracked':len(rows),'closed_by_pct':rates,'eligible_by_horizon':denominators,
        'mean_close_min':round(statistics.mean(times),2) if times else None,
        'median_close_min':statistics.median(times) if times else None,
        'not_closed_24h_pct':100-rates['1440'] if rates['1440'] is not None else None}


def history_line(path, symbol, category='POSITIVE'):
    with session(path) as db:
        if category == 'NEGATIVE':
            negative = negative_statistics(db, symbol)
            return (f"История NEGATIVE {symbol[:-4]}: {negative['count']} эпизодов; "
                    f"наблюдалось ≥0%: {negative['reached_zero']}; "
                    f"положительный spread: {negative['became_positive']}.")
        data = statistics_for(db, symbol, 'POSITIVE')
    n = data['complete']
    if n < MIN_HISTORY:
        return f"История {symbol[:-4]}: {n} полных наблюдений; недостаточно истории."
    return (f"История {symbol[:-4]}: {n} наблюдений; "
            f"{data['closed_by_pct']['15']}% закрылись ≤0,10% к 15 мин; "
            f"медиана первого наблюдения закрытия {data['median_close_min']} мин.")


def negative_statistics(db, symbol=None):
    query = "SELECT * FROM episodes WHERE direction='spot_buy/futures_short' AND CAST(executable_spread_pct AS REAL)<0"
    params = ()
    if symbol:
        query += ' AND symbol=?'
        params = (symbol,)
    rows = db.execute(query, params).fetchall()
    reached, positive, times = 0, 0, []
    entries = [Decimal(r['executable_spread_pct']) for r in rows]
    for row in rows:
        samples = db.execute("SELECT * FROM checkpoints WHERE episode_id=? AND status='observed' ORDER BY sampled_at", (row['id'],)).fetchall()
        nonnegative = [c for c in samples if Decimal(c['executable_spread_pct']) >= 0]
        if nonnegative:
            reached += 1
            times.append((nonnegative[0]['sampled_at'] - row['started_at']) / 60)
        if any(Decimal(c['executable_spread_pct']) > 0 for c in samples):
            positive += 1
    return {'count': len(rows), 'mean_entry': sum(entries) / len(entries) if entries else None,
            'worst_entry': min(entries) if entries else None,
            'reached_zero': reached, 'became_positive': positive,
            'mean_zero_min': statistics.mean(times) if len(times) >= MIN_HISTORY else None,
            'zero_samples': len(times)}


def stats_line(path=None, symbol=None):
    with session(path) as db:
        positive = statistics_for(db, symbol, 'POSITIVE')
        negative = negative_statistics(db, symbol)
        lifecycle = {c:lifecycle_statistics(db,symbol,c) for c in ('POSITIVE','NEGATIVE')}
    value = lambda x: 'нет данных' if x is None else f'{x:.2f}'
    rates = positive['closed_by_pct']
    return (f"SPOT → FUTURES — PAPER / VIRTUAL: {symbol or 'Binance / Gate / BingX'}\n"
            f"POSITIVE: {positive['observations']} эпизодов; полных: {positive['complete']}; неполных: {positive['incomplete']}.\n"
            f"NEGATIVE: {negative['count']} эпизодов.\n"
            + f"Средний spread входа по стакану: {value(negative['mean_entry'])}%; самый отрицательный: {value(negative['worst_entry'])}%.\n"
            + f"Дошли до 0% или выше: {negative['reached_zero']}; перешли в положительный: {negative['became_positive']}.\n"
            + f"Среднее время первого наблюдения ≥0%: {value(negative['mean_zero_min'])} мин "
            + f"(наблюдений: {negative['zero_samples']}; минимум для среднего: {MIN_HISTORY}).\n"
            + '\n'.join(f"{c} — схождение к 1/5/15/30/60 мин, 3/6/12/24 ч: "
                + ' / '.join('нет данных' if d['closed_by_pct'][str(t)] is None else f"{d['closed_by_pct'][str(t)]}% (n={d['eligible_by_horizon'][str(t)]})" for t in HORIZONS+EXTENDED_HORIZONS)
                + f"; среднее {value(d['mean_close_min'])} мин; медиана {value(d['median_close_min'])} мин; не сошлись за 24 ч {value(d['not_closed_24h_pct'])}%." for c,d in lifecycle.items())
            + "\nТолько фактические контрольные точки; пропущенные замеры не восстанавливаются. Достижение порога спреда не означает реализованную прибыль.")
