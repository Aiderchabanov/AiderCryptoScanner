"""Independent persistent PAPER accounts. No exchange/network or order API."""
import hashlib
import json
import logging
import time
from decimal import Decimal as D
import paper

BALANCE = D('10000')
USED_FRACTION = D('0.50')
MIGRATION = 'PAPER_EXCHANGE_CAPITAL_10000_2026_10'
VENUES = ('Binance', 'Gate', 'BingX', 'MEXC')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS scanner_exchange_accounts (
 exchange TEXT PRIMARY KEY, initial_balance TEXT NOT NULL,
 created_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS scanner_capital_ledger (
 event_key TEXT PRIMARY KEY, exchange TEXT NOT NULL, kind TEXT NOT NULL,
 amount TEXT NOT NULL, balance_before TEXT, balance_after TEXT NOT NULL,
 recorded_at DOUBLE PRECISION NOT NULL, reason TEXT NOT NULL, details_json TEXT NOT NULL
);
'''


def ensure(db):
    for statement in SCHEMA.split(';'):
        if statement.strip():
            db.execute(statement)


def active(api):
    # Configuration/auth readiness only, never enable a client or send requests.
    return tuple(name for name in VENUES if api.credentials_ready(name))


def history_hash(db):
    values = {}
    for table, order in (('episodes', 'id'), ('virtual_state', 'episode_id')):
        values[table] = [dict(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY {order}').fetchall()]
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()


def migrate(names, path=None):
    """Initialize only proven active venues once, leaving history byte-for-byte intact."""
    import virtual
    names = tuple(names)
    if any(name not in VENUES for name in names):
        raise ValueError('Unsupported venue; activation requires a separate change')
    with paper.session(path) as db:
        virtual.ensure(db)
        virtual.lock(db)
        if not getattr(db, 'is_postgres', False):
            db.execute('BEGIN IMMEDIATE')
        ensure(db)
        before = history_hash(db)
        created = []
        for name in names:
            if db.execute('SELECT 1 FROM scanner_exchange_accounts WHERE exchange=?', (name,)).fetchone():
                continue
            at = time.time()
            db.execute('INSERT INTO scanner_exchange_accounts VALUES (?,?,?)', (name, str(BALANCE), at))
            details = {'balance_before': None, 'balance_after': str(BALANCE),
                       'previous_model': 'legacy shared pool; no individual venue balance',
                       'legacy_initial_deposit': str(virtual.deposit()),
                       'legacy_capital_limit': str(virtual.working_capital_limit()),
                       'history_hash_before': before,
                       'history_hash_after': before,
                       'scope': 'scanner only; no observer account access',
                       'limit_change': 'replace shared absolute cap with 50% per exchange',
                       'pnl_cutover': 'only subsequent fully known close events; no historical reallocation'}
            db.execute('INSERT INTO scanner_capital_ledger VALUES (?,?,?,?,?,?,?,?,?)',
                       (MIGRATION + ':' + name, name, 'ACCOUNT_INIT', '10000', None, '10000', at, MIGRATION, json.dumps(details, sort_keys=True)))
            created.append(name)
        after = history_hash(db)
        if after != before:
            raise RuntimeError('Capital migration altered episode history')
        if created:
            logging.info('PAPER capital migration: exchanges=%s history_hash_before=%s history_hash_after=%s history_unchanged=true', created, before, after)
        return created


def balances(db):
    ensure(db)
    result = {r['exchange']: D(r['initial_balance']) for r in db.execute('SELECT * FROM scanner_exchange_accounts').fetchall()}
    for row in db.execute("SELECT exchange,amount FROM scanner_capital_ledger WHERE kind='REALIZED_LEG_PNL'").fetchall():
        result[row['exchange']] += D(row['amount'])
    return result


def occupied(db):
    import virtual
    result = {}
    rows = db.execute("SELECT e.*,v.state AS virtual_status FROM episodes e LEFT JOIN virtual_state v ON v.episode_id=e.id WHERE e.direction='spot_buy/futures_short'").fetchall()
    for e in rows:
        if e['virtual_status'] != 'open' and not (e['virtual_status'] is None and e['first_close_min'] is None):
            continue
        legs = ((e['spot_exchange'], virtual.number(e['actual_spot_usdt'], True)),
                (e['futures_exchange'], virtual.number(e['actual_futures_usdt'], True) / virtual.episode_leverage(db, e['id'])))
        for name, amount in legs:
            result[name] = result.get(name, D(0)) + amount
    return result


def admits(db, item):
    import virtual
    funds = balances(db)
    if not funds:
        # Legacy isolated SQLite fixtures remain supported. Production fails closed
        # until startup has verified active clients and committed account migration.
        return not getattr(db, 'is_postgres', False) and virtual.used(db) + virtual.capital(item['spot_cost'], item['future_notional']) <= virtual.working_capital_limit()
    used = occupied(db)
    needs = {}
    for name, amount in ((item['spot'], virtual.number(item['spot_cost'], True)),
                         (item['future'], virtual.number(item['future_notional'], True) / virtual.leverage())):
        needs[name] = needs.get(name, D(0)) + amount
    return all(name in funds and used.get(name, D(0)) + amount <= max(D(0), funds[name] * USED_FRACTION)
               for name, amount in needs.items())


def closed(db, episode, quote):
    """Book known leg P&L once in the same transaction as normal closure."""
    import virtual
    ensure(db)
    if virtual.realized_result(quote) is None:
        return
    amounts = {}
    for name, amount in ((episode['spot_exchange'], D(quote['spot_pnl'])),
                         (episode['futures_exchange'], D(quote['futures_pnl']) + D(quote['funding_realized_usdt']))):
        amounts[name] = amounts.get(name, D(0)) + amount
    funds = balances(db)
    for name, amount in amounts.items():
        if name not in funds:
            continue
        key = f"close:{episode['episode_id']}:{name}"
        if db.execute('SELECT 1 FROM scanner_capital_ledger WHERE event_key=?', (key,)).fetchone():
            continue
        db.execute('INSERT INTO scanner_capital_ledger VALUES (?,?,?,?,?,?,?,?,?)',
                   (key, name, 'REALIZED_LEG_PNL', str(amount), str(funds[name]), str(funds[name] + amount),
                    time.time(), 'CONFIRMED_VIRTUAL_CLOSE', json.dumps({'episode_id': episode['episode_id'], 'fees': 'included in saved leg P&L'})))


def status(names, path=None):
    import virtual
    with paper.session(path) as db:
        virtual.ensure(db)
        funds, used = balances(db), occupied(db)
        rows = db.execute("SELECT e.spot_exchange,e.futures_exchange FROM episodes e JOIN virtual_state v ON v.episode_id=e.id WHERE v.state='open'").fetchall()
        ledger_count = db.execute('SELECT COUNT(*) AS n FROM scanner_capital_ledger').fetchone()['n']
        migrations = [{'exchange': r['exchange'], 'migration_timestamp': r['recorded_at'],
                       'migration_reason': r['reason'], 'balance_before': r['balance_before'],
                       'balance_after': r['balance_after'], **json.loads(r['details_json'])}
                      for r in db.execute("SELECT * FROM scanner_capital_ledger WHERE kind='ACCOUNT_INIT' ORDER BY exchange").fetchall()]
        accounts = [{'exchange': name, 'active': name in names, 'virtual_balance': str(balance),
                     'used_capital': str(used.get(name, D(0))),
                     'free_capital': str(max(D(0), balance - used.get(name, D(0)))),
                     'max_allowed_used_capital': str(max(D(0), balance * USED_FRACTION)),
                     'admission_available_capital': str(max(D(0), balance * USED_FRACTION - used.get(name, D(0)))),
                     'open_episodes_using_exchange': sum(name in (r['spot_exchange'], r['futures_exchange']) for r in rows)}
                    for name, balance in sorted(funds.items())]
    return {'capital_model': 'independent persistent per-exchange PAPER accounts',
            'capital_limit_rule': 'each exchange: used + new leg capital <= current exchange balance * 0.50; legacy shared 500 cap replaced',
            'TARGET_LEG_USDT': str(__import__('basis').TARGET_LEG_USDT),
            'active_exchanges': list(names), 'accounts': accounts, 'ledger_count': ledger_count,
            'migrations': migrations,
            'real_trading_enabled': False, 'real_orders_executed': False,
            'transfers_executed': False, 'withdrawals_executed': False}
