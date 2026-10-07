"""Persistent PAPER lifecycle. Exchange calls are read-only; no order API exists here."""
import json
import binance_io
import bingx_diagnostics as bxdiag
import logging
import math
import os
import secrets
import time
from decimal import Decimal
import paper
import entry_diagnostics as diagnostics

D = Decimal
SCHEMA = '''
CREATE TABLE IF NOT EXISTS virtual_state (
 episode_id BIGINT PRIMARY KEY REFERENCES episodes(id), parent_id BIGINT,
 state TEXT NOT NULL, used_capital TEXT NOT NULL, last_notice_spread TEXT NOT NULL,
 last_warning INTEGER NOT NULL DEFAULT 0, current_json TEXT, sampled_at DOUBLE PRECISION,
 closed_at DOUBLE PRECISION, close_json TEXT, anomaly TEXT
);
CREATE TABLE IF NOT EXISTS virtual_proposals (
 token TEXT PRIMARY KEY, episode_id BIGINT NOT NULL, action TEXT NOT NULL,
 chat_id TEXT NOT NULL, user_id TEXT NOT NULL, expires_at DOUBLE PRECISION NOT NULL,
 quote_json TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS virtual_events (
 event_key TEXT PRIMARY KEY, episode_id BIGINT NOT NULL, body TEXT NOT NULL,
 buttons_json TEXT, state TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS virtual_checkpoint_details (
 episode_id BIGINT NOT NULL REFERENCES episodes(id), horizon_min INTEGER NOT NULL,
 sampled_at DOUBLE PRECISION NOT NULL, snapshot_json TEXT NOT NULL,
 PRIMARY KEY (episode_id,horizon_min)
);
CREATE TABLE IF NOT EXISTS virtual_meta (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS virtual_metrics (
 episode_id BIGINT PRIMARY KEY REFERENCES episodes(id),
 tracking_started_at DOUBLE PRECISION NOT NULL, max_spread TEXT NOT NULL,
 min_spread TEXT NOT NULL, max_expansion_pp TEXT NOT NULL,
 convergence_seconds DOUBLE PRECISION, latest_json TEXT
);
'''


def ensure(db):
    if getattr(db, 'is_postgres', False):
        # Serialize additive DDL across scanner, observer and command threads.
        db.execute("SELECT pg_advisory_xact_lock(hashtext('virtual-schema'))")
    for statement in SCHEMA.split(';'):
        if statement.strip():
            db.execute(statement)


def lock(db):
    if getattr(db, 'is_postgres', False):
        db.execute("SELECT pg_advisory_xact_lock(hashtext('virtual-global-capital'))")


def deposit():
    value = D(os.getenv('PAPER_DEPOSIT_USDT', '1000'))
    if not value.is_finite() or value <= 0:
        raise ValueError('Invalid virtual deposit')
    return value


def working_capital_limit():
    return min(deposit() / 2, D(500))


def number(value, positive=False):
    value = D(str(value))
    if not value.is_finite() or (positive and value <= 0):
        raise ValueError('Unknown or invalid mandatory value')
    return value


_leverage_logged = None

def leverage():
    global _leverage_logged
    raw=os.getenv('PAPER_FUTURES_LEVERAGE')
    value=number('1' if raw is None else raw,True)
    if value<1:raise ValueError('PAPER_FUTURES_LEVERAGE must be >= 1')
    mode='default (PAPER_FUTURES_LEVERAGE absent)' if raw is None else 'configured'
    if _leverage_logged!=(str(value),mode):
        logging.info('PAPER Futures leverage: %sx; %s; virtual margin accounting only',value,mode)
        _leverage_logged=(str(value),mode)
    return value


def capital(spot,notional,lev=None):
    return number(spot,True)+number(notional,True)/(leverage() if lev is None else number(lev,True))


def episode_leverage(db,ident):
    row=db.execute('SELECT value FROM virtual_meta WHERE name=?',(f'capital_leverage:{ident}',)).fetchone()
    return number(row['value'],True) if row else leverage()


def used(db):
    # Actual Spot capital plus Futures margin; entry leverage survives restarts.
    rows = db.execute("SELECT e.*, v.state AS virtual_status FROM episodes e LEFT JOIN virtual_state v ON v.episode_id=e.id WHERE e.direction='spot_buy/futures_short'").fetchall()
    return sum((capital(r['actual_spot_usdt'],r['actual_futures_usdt'],episode_leverage(db,r['id']))
                for r in rows if r['virtual_status'] == 'open' or
                (r['virtual_status'] is None and r['first_close_min'] is None)), D(0))


def register(db, ident, item, parent=None):
    lev=leverage()
    reserved = capital(item['spot_cost'],item['future_notional'],lev)
    db.execute('''INSERT INTO virtual_state (episode_id,parent_id,state,used_capital,last_notice_spread,anomaly)
                  VALUES (?,?,'open',?,?,?)''',
               (ident, parent, str(reserved), str(item['executable_spread_pct']), item.get('anomaly')))
    db.execute('INSERT INTO virtual_meta (name,value) VALUES (?,?)',(f'capital_leverage:{ident}',str(lev)))
    seed_metrics(db, ident, item['executable_spread_pct'])


def bootstrap(path=None):
    with paper.session(path) as db:
        ensure(db); lock(db)
        active = db.execute("SELECT e.* FROM episodes e LEFT JOIN virtual_state v ON v.episode_id=e.id WHERE e.direction='spot_buy/futures_short' AND e.first_close_min IS NULL AND v.episode_id IS NULL").fetchall()
        for e in active:
            db.execute("INSERT INTO virtual_state (episode_id,state,used_capital,last_notice_spread) VALUES (?,'open',?,?) ON CONFLICT (episode_id) DO NOTHING", (e['id'],str(capital(e['actual_spot_usdt'],e['actual_futures_usdt'])),e['executable_spread_pct']))
        # Upgrade only fully known saved close snapshots; never reconstruct prices/funding.
        for closed in db.execute("SELECT episode_id,close_json FROM virtual_state WHERE state='closed'").fetchall():
            try:
                snapshot=json.loads(closed['close_json'] or '{}')
                if 'realized_net_pnl_usdt' not in snapshot:
                    snapshot['realized_net_pnl_usdt']=realized_result(snapshot)
                    db.execute('UPDATE virtual_state SET close_json=? WHERE episode_id=?',(json.dumps(snapshot),closed['episode_id']))
            except (ValueError, TypeError):
                pass
        leverage() # Explicit default/configured startup log.
        for e in db.execute("SELECT e.* FROM episodes e JOIN virtual_state v ON e.id=v.episode_id WHERE v.state='open'").fetchall():
            lev=episode_leverage(db,e['id'])
            db.execute('INSERT INTO virtual_meta (name,value) VALUES (?,?) ON CONFLICT (name) DO NOTHING',(f"capital_leverage:{e['id']}",str(lev)))
            db.execute('UPDATE virtual_state SET used_capital=? WHERE episode_id=?',(str(capital(e['actual_spot_usdt'],e['actual_futures_usdt'],lev)),e['id']))
            seed_metrics(db,e['id'],e['executable_spread_pct'])
            for minute in paper.EXTENDED_HORIZONS:
                due=e['started_at']+minute*60
                if due>=time.time():
                    db.execute("INSERT INTO checkpoints (episode_id,horizon_min,due_at,status) VALUES (?,?,?,'pending') ON CONFLICT (episode_id,horizon_min) DO NOTHING",(e['id'],minute,due))
        recovered = [dict(r) for r in db.execute("SELECT episode_id,last_notice_spread,last_warning FROM virtual_state WHERE state='open' ORDER BY episode_id").fetchall()]
        logging.info('Virtual recovery: %s open episodes; used capital %s / %s USDT; notification levels %s', len(recovered), used(db), working_capital_limit(), recovered)

    a=accounting(path)
    logging.info('Virtual accounting: confirmed_realized_pnl=%s; current_virtual_balance=%s; closed_complete=%s; closed_incomplete=%s; open=%s; used_capital=%s; complete_ids=%s; incomplete_ids=%s',a['confirmed_realized_pnl'],a['current_virtual_balance'],a['closed_complete'],a['incomplete_closed_pnl_count'],a['open_episodes'],a['used_capital'],a['closed_complete_ids'],a['closed_incomplete_ids'])

def rows(path=None):
    with paper.session(path) as db:
        ensure(db)
        return [dict(r) for r in db.execute("SELECT e.*,v.* FROM episodes e JOIN virtual_state v ON e.id=v.episode_id WHERE v.state='open' ORDER BY e.id").fetchall()]


def load(ident, path=None):
    return next((r for r in rows(path) if r['episode_id'] == ident), None)


def checked_book(book):
    asks, bids = book
    if not asks or not bids:
        raise ValueError('Both sides of orderbook required')
    asks = [(number(p, True), number(q, True)) for p,q in asks]
    bids = [(number(p, True), number(q, True)) for p,q in bids]
    if len({p for p,q in asks}) != len(asks) or len({p for p,q in bids}) != len(bids) or asks != sorted(asks) or bids != sorted(bids, reverse=True) or bids[0][0] >= asks[0][0]:
        raise ValueError('Malformed/crossed orderbook')
    return asks, bids


def quote(api, episode):
    started = time.monotonic()
    qty = number(episode['quantity'], True)
    snap = json.loads(episode['cost_snapshot_json'])
    multiplier = number(snap['multiplier'], True)
    spot_asks, spot_bids = checked_book(api.orderbook(episode['spot_exchange'], episode['symbol']))
    future_asks, future_bids = checked_book(api.basis.futures_book(api, episode['futures_exchange'], episode['symbol'], multiplier))
    spot_fee = number(api.fee(episode['spot_exchange'], episode['symbol']))
    future_fee = number(api.basis.futures_fee(api, episode['futures_exchange'], episode['symbol']))
    entry_future_fee = number(snap['future_fee'])
    if any(not D(0) <= f < D(1) for f in (spot_fee, future_fee, entry_future_fee)):
        raise ValueError('Fee unavailable')
    spot_sale = api.sell_for_usdt(spot_bids, qty)
    cover = api.basis.spot_cost(future_asks, qty, api)
    spot_buy = api.basis.spot_cost(spot_asks, qty, api)
    short = api.sell_for_usdt(future_bids, qty)
    if any(v is None for v in (spot_sale, cover, spot_buy, short)) or time.monotonic()-started > 30:
        raise ValueError('Fresh executable depth unavailable')
    spot_sale, cover, spot_buy, short = [number(v, True) for v in (spot_sale, cover, spot_buy, short)]
    # Spot entry cost already includes base-asset entry fee; do not deduct twice.
    entry_cost = number(episode['actual_spot_usdt'], True)
    short_entry = number(episode['actual_futures_usdt'], True)
    spot_entry_fee = max(D(0),entry_cost-qty*number(episode['spot_vwap'],True))
    spot_exit_fee = spot_sale*spot_fee
    futures_entry_fee = short_entry*entry_future_fee
    futures_exit_fee = cover*future_fee
    # Preserve the existing net calculation; entry Spot fee is already in cost.
    fees = futures_entry_fee+spot_exit_fee+futures_exit_fee
    spot_pnl = spot_sale-spot_exit_fee-entry_cost
    futures_pnl = short_entry-cover-futures_entry_fee-futures_exit_fee
    trading_net = spot_pnl+futures_pnl
    slippage = {
        'spot_entry_usdt':snap.get('spot_slippage_usdt'),
        'futures_entry_usdt':snap.get('futures_slippage_usdt'),
        'spot_exit_usdt':str(max(D(0),qty*spot_bids[0][0]-spot_sale)),
        'futures_exit_usdt':str(max(D(0),cover-qty*future_asks[0][0]))}

    funding_known = time.time() < float(episode['next_funding_at'])
    spread = (short / spot_buy - 1) * 100
    return {'spot_pnl':str(spot_pnl),'futures_pnl':str(futures_pnl),
            'funding_rate_at_entry':episode['funding_rate'],
            'funding_realized_usdt':'0' if funding_known else None,
            'total_trading_fees':str(fees+spot_entry_fee),
            'fee_breakdown':{'spot_entry':str(spot_entry_fee),'spot_exit':str(spot_exit_fee),'futures_entry':str(futures_entry_fee),'futures_exit':str(futures_exit_fee)},
            'slippage':slippage, 'at':time.time(), 'spread':str(spread), 'spot_bid':str(spot_bids[0][0]),
            'spot_ask':str(spot_asks[0][0]),'futures_bid':str(future_bids[0][0]),
            'spot_entry_vwap_now':str(spot_buy/qty),'future_entry_vwap_now':str(short/qty),
            # Diagnostic only: use the book already fetched, never another GET.
            'spot_depth_usdt':str(sum((p*q for p,q in spot_asks),D(0))),
            'futures_depth_usdt':str(sum((p*q for p,q in future_bids),D(0))),
            'spot_depth_quantity':str(sum((q for p,q in spot_asks),D(0))),
            'futures_depth_quantity':str(sum((q for p,q in future_bids),D(0))),
            'raw_spread':str((future_bids[0][0]/spot_asks[0][0]-1)*100),
            'futures_ask':str(future_asks[0][0]), 'spot_exit':str(spot_sale/qty),
            'future_exit':str(cover/qty), 'fees':str(fees), 'trading_net':str(trading_net),
            'net_pnl':str(trading_net) if funding_known else None,
            'funding_status':'no settlement crossed' if funding_known else 'UNKNOWN_SETTLEMENT_PNL'}


def pnl_line(q):
    suffix = '' if q['net_pnl'] is not None else '; funding не подтверждён, полный Net P&L неизвестен'
    return f"Spread {D(q['spread']):+.4f}%; торговый Net P&L {D(q['trading_net']):+.4f} USDT{suffix}"


def event(db, key, ident, body, buttons=None):
    db.execute('''INSERT INTO virtual_events (event_key,episode_id,body,buttons_json)
                  VALUES (?,?,?,?) ON CONFLICT (event_key) DO NOTHING''',
               (key,ident,body,json.dumps(buttons) if buttons else None))


def seed_metrics(db,ident,spread):
    db.execute("""INSERT INTO virtual_metrics (episode_id,tracking_started_at,max_spread,min_spread,max_expansion_pp)
                VALUES (?,?,?,?, '0') ON CONFLICT (episode_id) DO NOTHING""",(ident,time.time(),str(spread),str(spread)))


def update_metrics(db,e,q,converged=False):
    seed_metrics(db,e['episode_id'],e['executable_spread_pct'])
    old=db.execute('SELECT * FROM virtual_metrics WHERE episode_id=?',(e['episode_id'],)).fetchone()
    spread=number(q['spread']);entry=number(e['executable_spread_pct'])
    db.execute("""UPDATE virtual_metrics SET max_spread=?,min_spread=?,max_expansion_pp=?,
                  convergence_seconds=COALESCE(convergence_seconds,?),latest_json=? WHERE episode_id=?""",
               (str(max(number(old['max_spread']),spread)),str(min(number(old['min_spread']),spread)),
                str(max(number(old['max_expansion_pp']),spread-entry)),
                max(0,q['at']-e['started_at']) if converged else None,json.dumps(q),e['episode_id']))


def realized_result(q):
    # Complete Net P&L only; unknown settlement is never replaced with zero.
    if q.get('funding_status') == 'UNKNOWN_SETTLEMENT_PNL' or q.get('net_pnl') is None:
        return None
    try:
        if q.get('funding_realized_usdt') is None:
            return None
        for key in ('spot_pnl', 'futures_pnl', 'funding_realized_usdt', 'total_trading_fees'):
            number(q[key])
        result=number(q['net_pnl'])
        if result != number(q['spot_pnl'])+number(q['futures_pnl'])+number(q['funding_realized_usdt']):
            return None
        return str(result)
    except (KeyError, ValueError, ArithmeticError):
        return None


def accounting(path=None, api=None):
    with paper.session(path) as db:
        ensure(db)
        closed=db.execute("SELECT episode_id,close_json FROM virtual_state WHERE state='closed'").fetchall()
        occupied=used(db)
        opened=[dict(r) for r in db.execute("SELECT e.*,v.* FROM episodes e JOIN virtual_state v ON v.episode_id=e.id WHERE v.state='open'").fetchall()]
    confirmed=D(0); complete=0; complete_ids=[]; incomplete_ids=[r['episode_id'] for r in closed]
    for row in closed:
        try:
            q=json.loads(row['close_json'] or '{}')
            value=q.get('realized_net_pnl_usdt')
            if value is None or realized_result(q) is None or number(value)!=number(realized_result(q)):
                continue
            confirmed+=number(value);complete+=1
            complete_ids.append(row['episode_id']);incomplete_ids.remove(row['episode_id'])
        except (ValueError, TypeError, ArithmeticError):
            continue
    unrealized=D(0);unknown=0
    for e in opened:
        if api is None:
            unknown+=1;continue
        try:
            q=quote(api,e)
            if time.time()-q['at']>30 or q['net_pnl'] is None:
                unknown+=1;continue
            unrealized+=number(q['net_pnl'])
        except Exception:
            unknown+=1
    limit=working_capital_limit()
    return {'confirmed_realized_pnl':confirmed,'cumulative_realized_pnl':confirmed,
            'current_virtual_balance':deposit()+confirmed,'initial_virtual_deposit':deposit(),
            'incomplete_closed_pnl_count':len(closed)-complete,'closed_complete':complete,
            'closed_complete_ids':complete_ids,'closed_incomplete_ids':incomplete_ids,
            'closed_complete_count':complete,'closed_incomplete_count':len(closed)-complete,
            'open_episodes_count':len(opened),'open_episodes':len(opened),'unrealized_pnl_open':unrealized if unknown==0 else None,
            'known_unrealized_subtotal':unrealized,'unrealized_unknown_count':unknown,
            'used_capital':occupied,'capital_limit':limit,'free_capital':max(D(0),limit-occupied)}


def status_message(api, path=None):
    """Read-only status projection; migrated accounts are the capital authority."""
    import exchange_capital
    a = accounting(path, api)
    names = exchange_capital.active(api) if hasattr(api, 'credentials_ready') else ()
    accounts = exchange_capital.status(names, path)['accounts']
    unrealized=(f"{a['unrealized_pnl_open']:+.2f} USDT" if a['unrealized_pnl_open'] is not None
                else f"UNKNOWN ({a['unrealized_unknown_count']} incomplete; known subtotal {a['known_unrealized_subtotal']:+.2f} USDT)")
    if accounts:
        lines = ["📊 VIRTUAL ACCOUNT — PAPER ONLY", "Independent per-exchange accounts"]
        for row in accounts:
            lines.extend([
                f"\n{row['exchange']} ({'ACTIVE' if row['active'] else 'INACTIVE'}):",
                f"Balance: {D(row['virtual_balance']):.2f} USDT",
                f"Used: {D(row['used_capital']):.2f} / {D(row['max_allowed_used_capital']):.2f} USDT (50% limit)",
                f"Free: {D(row['free_capital']):.2f} USDT",
                f"Available for new legs: {D(row['admission_available_capital']):.2f} USDT",
                f"Open episodes using exchange: {row['open_episodes_using_exchange']}"])
        lines.extend([
            f"\nConfirmed realized P&L (all history): {a['confirmed_realized_pnl']:+.2f} USDT",
            f"Unrealized P&L open: {unrealized}",
            f"Open episodes: {a['open_episodes']}",
            f"Closed complete: {a['closed_complete']} {a['closed_complete_ids']}",
            f"Closed incomplete: {a['incomplete_closed_pnl_count']} {a['closed_incomplete_ids']}",
            "Free = balance minus reserved capital. Available for new legs = unused 50% limit.",
            "Exchange balances include confirmed leg results after migration; earlier P&L remains in historical totals."])
        return '\n'.join(lines)
    # Isolated pre-migration SQLite history retains its original display. Never
    # present a legacy pool as active production capital while migration is pending.
    if path is None:
        return "📊 VIRTUAL ACCOUNT — PAPER ONLY\nPer-exchange capital unavailable; account migration not confirmed."
    return (
        f"📊 VIRTUAL ACCOUNT — PAPER ONLY\nInitial deposit: {a['initial_virtual_deposit']:.2f} USDT\n"
        f"Confirmed realized P&L: {a['confirmed_realized_pnl']:+.2f} USDT\n"
        f"Current virtual balance: {a['current_virtual_balance']:.2f} USDT\n"
        f"Unrealized P&L open: {unrealized}\nOpen episodes: {a['open_episodes']}\n"
        f"Closed complete: {a['closed_complete']} {a['closed_complete_ids']}\nClosed incomplete: {a['incomplete_closed_pnl_count']} {a['closed_incomplete_ids']}\n"
        f"Used capital: {a['used_capital']:.2f} / {a['capital_limit']:.2f} USDT\n"
        f"Free capital: {a['free_capital']:.2f} USDT\n"
        "Balance includes confirmed results only; free capital is the unused working limit.")


def status(api,chat_id,path=None):
    api.telegram(status_message(api, path), chat_id)


def close_db(db, e, q):
    lock(db)
    q=dict(q)
    q['realized_net_pnl_usdt']=realized_result(q)
    result = db.execute("UPDATE virtual_state SET state='closed',closed_at=?,close_json=?,current_json=? WHERE episode_id=? AND state='open'",(q['at'],json.dumps(q),json.dumps(q),e['episode_id']))
    if result.rowcount != 1:
        return False
    import exchange_capital
    exchange_capital.closed(db, e, q)
    spread=number(q['spread'])
    converged=abs(spread)<=paper.CLOSED_PCT
    update_metrics(db,e,q,converged)
    db.execute("UPDATE episodes SET status='closed', first_close_min=COALESCE(first_close_min,?) WHERE id=?",(max(1,math.ceil((q['at']-e['started_at'])/60)), e['episode_id']))
    if abs(D(q['spread'])) <= paper.CLOSED_PCT:
        db.execute('INSERT INTO virtual_meta (name,value) VALUES (?,?) ON CONFLICT (name) DO NOTHING',(f"gapreset:{e['episode_id']}",'1'))
    event(db, f"close:{e['episode_id']}",e['episode_id'], f"✅ VIRTUAL CLOSED #{e['episode_id']} {e['symbol']}\nSpot SELL {q['spot_exit']}; Futures BUY {q['future_exit']}\n{pnl_line(q)}\nPAPER ONLY")
    return True



def spike_snapshot(db, e, q, funding):
    """Retain a strong spike from existing observations only; no API access."""
    entry=number(e['executable_spread_pct'])
    expansion=number(q['spread'])-entry
    if expansion < D(5):
        return
    def optional_number(key):
        try:
            return number(q[key]) if q.get(key) is not None else None
        except (ValueError,TypeError,ArithmeticError):
            return None
    def age_flag(key):
        age=optional_number(key)
        return 'unknown' if age is None else age>D(30000) or age<D(-5000)
    def depth_flag(key):
        depth=optional_number(key)
        return 'unknown' if depth is None else depth<number(e['quantity'],True)
    difference=optional_number('quote_time_difference_ms')
    fresh=0 <= time.time()-funding.get('at',0) <=30 and funding.get('next_at',0)>time.time()
    data={'timestamp':q['at'],'episode_id':e['episode_id'],'symbol':e['symbol'],
          'spot_exchange':e['spot_exchange'],'futures_exchange':e['futures_exchange'],
          'entry_spread':str(entry),'current_spread':q['spread'],'expansion':str(expansion),
          'spot_best_ask':q.get('spot_ask'),'spot_executable_avg':q.get('spot_entry_vwap_now'),
          'spot_depth_usdt':q.get('spot_depth_usdt'),
          'futures_best_bid':q.get('futures_bid'),'futures_executable_avg':q.get('future_entry_vwap_now'),
          'futures_depth_usdt':q.get('futures_depth_usdt'),
          'spot_quote_age_ms':q.get('spot_quote_age_ms'),
          'futures_quote_age_ms':q.get('futures_quote_age_ms'),
          'quote_time_difference_ms':str(difference) if difference is not None else None,
          'trading_net_pnl':q.get('trading_net'),
          'funding':funding.get('rate') if fresh else None,
          'funding_settlement_status':q.get('funding_status','UNKNOWN_SETTLEMENT_PNL'),
          'stale_spot_quote':age_flag('spot_quote_age_ms'),
          'stale_futures_quote':age_flag('futures_quote_age_ms'),
          'quote_time_mismatch':'unknown' if difference is None else abs(difference)>D(30000),
          'thin_spot_depth':depth_flag('spot_depth_quantity'),
          'thin_futures_depth':depth_flag('futures_depth_quantity'),
          'depth_flag_basis':'observed entry-side quantity below episode quantity; not a general liquidity assessment',
          'quote_time_mismatch_threshold_ms':30000}
    result=db.execute('INSERT INTO virtual_meta (name,value) VALUES (?,?) ON CONFLICT (name) DO NOTHING',
                      (f"spike_snapshot:{e['episode_id']}:{q['at']}",json.dumps(data)))
    if result.rowcount:
        logging.info('Virtual spike diagnostic saved: episode #%s symbol=%s expansion=%s p.p.; reused existing quote; no additional API request',e['episode_id'],e['symbol'],expansion)


def risk_alert(db, e, q):
    """Observe only: reuse fresh executable quote, persist crossing/rearm state."""
    if not 0 <= time.time()-q['at'] <= 30:
        return
    ident=e['episode_id']
    current=db.execute('SELECT state FROM virtual_state WHERE episode_id=?',(ident,)).fetchone()
    if not current or current['state']!='open':
        return
    key=f'risk_levels:{ident}'
    saved=db.execute('SELECT value FROM virtual_meta WHERE name=?',(key,)).fetchone()
    state=json.loads(saved['value']) if saved else {'pnl_warning_level':0,'spread_expansion_level':0,'generation':0}
    # Ignore repeated/out-of-order snapshots: they must not reset notified state.
    if q['at'] <= state.get('last_sample_at',0):
        return
    entry=number(e['executable_spread_pct'])
    spread=number(q['spread'])
    expansion=spread-entry
    spread_level=max(0,int(expansion))  # Positive widening in percentage points.
    # Migrate existing state as already notified; no schema change or replay.
    spread_notified=set(state.get('spread_notified',range(1,state['spread_expansion_level']+1)))
    diagnostic_notified_before=tuple(spread_notified) if ident==22 else ()
    spread_notified={level for level in spread_notified if expansion>=D(level)-D('0.5')}
    crossed_spread=[level for level in range(1,spread_level+1) if level not in spread_notified]
    spread_notified.update(crossed_spread)
    state['spread_notified']=sorted(spread_notified)
    thresholds=(D('-.20'),D('-.50'),D('-1.00'))
    net=number(q['net_pnl']) if q.get('net_pnl') is not None else None
    # Migrate existing levels as already notified; restart cannot reintroduce them.
    notified=set(state.get('pnl_notified', [str(level) for level in thresholds[:state['pnl_warning_level']]]))
    previous_net=number(state['last_known_net_pnl']) if state.get('last_known_net_pnl') is not None else None
    new_pnl=[]
    if net is not None:
        for threshold in thresholds:
            name=str(threshold)
            if net > threshold:
                notified.discard(name)  # Confirmed recovery above this specific threshold.
            elif name not in notified and (previous_net is None or previous_net > threshold):
                new_pnl.append(threshold)
                notified.add(name)
        state['last_known_net_pnl']=str(net)
        state['pnl_warning_level']=sum(net<=level for level in thresholds)
    # UNKNOWN never resets a threshold or replaces the last known P&L with zero.
    state['pnl_notified']=[str(level) for level in thresholds if str(level) in notified]
    state['spread_expansion_level']=spread_level
    state['last_sample_at']=q['at']
    new_events=[]
    if crossed_spread:
        new_events.append(('spread',crossed_spread))
    if new_pnl:
        new_events.append(('pnl',new_pnl))
    if new_events or expansion>=D(5):
        live=db.execute('SELECT value FROM virtual_meta WHERE name=?',(f'funding_live:{ident}',)).fetchone()
        funding=json.loads(live['value']) if live else {}
        spike_snapshot(db,e,q,funding)
    if new_events:
        state['generation']+=1
        fresh=0 <= time.time()-funding.get('at',0) <= 30 and funding.get('next_at',0)>time.time()
        funding_text=f"{number(funding['rate'])*100:+.4f}%" if fresh else 'UNKNOWN (fresh funding unavailable)'
        remaining=int(funding['next_at']-time.time()) if fresh else None
        next_text=f"{remaining//60} min {remaining%60} sec" if remaining is not None else 'UNKNOWN'
        reasons=[]
        for kind,levels in new_events:
            if kind=='spread':
                reasons.append('SPREAD EXPANDED: '+', '.join(f'+{level}.0 p.p.' for level in levels))
            else:
                reasons.append('NET P&L crossed: '+', '.join(f'<= {level:.2f} USDT' for level in levels))
        net_text=f'{net:+.4f} USDT' if net is not None else 'UNKNOWN (funding settlement / full result unconfirmed)'
        body=(f"⚠️ VIRTUAL RISK ALERT #{ident} {e['symbol']}\n"+'\n'.join(reasons)+'\n'
              f"Spot: {e['spot_exchange']}\nFutures: {e['futures_exchange']}\n"
              f"Entry spread: {entry:+.4f}%\nCurrent executable spread: {spread:+.4f}%\n"
              f"Expansion: {expansion:+.4f} p.p.\nCurrent Net P&L: {net_text}\n"
              f"Funding: {funding_text}\nTime to next funding: {next_text}\n"
              "PAPER / VIRTUAL ONLY\nNo averaging / no real orders")
        event(db,f"risk:{ident}:{state['generation']}",ident,body)
        logging.info('Virtual risk alert queued: episode #%s; new_events=%s; pnl_warning_level=%s; spread_expansion_level=%s',ident,[(kind,[str(level) for level in levels]) for kind,levels in new_events],state['pnl_warning_level'],spread_level)
    db.execute("INSERT INTO virtual_meta (name,value) VALUES (?,?) ON CONFLICT (name) DO UPDATE SET value=excluded.value",(key,json.dumps(state)))
    if ident==22:
        import episode22_diagnostics
        episode22_diagnostics.emit(e,q,'hysteresis',diagnostic_notified_before,spread_notified,crossed_spread)

def record_bingx_offline(db, ident, exc, worker):
    """Record only a new natural failure; never manufacture an exit valuation."""
    if not bxdiag.is_offline(exc):
        return
    current = db.execute('SELECT state FROM virtual_state WHERE episode_id=?',(ident,)).fetchone()
    if not current or current['state'] != 'open':
        return
    at = time.time()
    snapshot = {k:exc.diagnostic_context.get(k,'UNKNOWN') for k in bxdiag.FIELDS}
    snapshot.update(timestamp=at, episode_id=ident, status='OPEN', worker=worker,
                    block_reason='BINGX_SYMBOL_OFFLINE', current_executable_quote=None,
                    current_executable_spread=None, trading_net_pnl=None, full_net_pnl=None)
    result=db.execute("UPDATE virtual_state SET current_json=NULL,sampled_at=? WHERE episode_id=? AND state='open'",(at,ident))
    if not result.rowcount:
        return
    db.execute("INSERT INTO virtual_meta (name,value) VALUES (?,?) ON CONFLICT (name) DO UPDATE SET value=excluded.value",
               (f'quote_availability:{ident}',json.dumps(snapshot)))
    logging.warning('BINGX_SYMBOL_OFFLINE_STATE: %s',json.dumps(snapshot,sort_keys=True))


def observe(api, path=None):
    # Manual closure of a continuing gap must not create a fresh entry next scan.
    with paper.session(path) as db:
        ensure(db)
        waiting = [dict(r) for r in db.execute("SELECT e.*,v.* FROM episodes e JOIN virtual_state v ON v.episode_id=e.id WHERE v.state='closed' AND NOT EXISTS (SELECT 1 FROM virtual_meta m WHERE m.name='gapreset:' || CAST(e.id AS TEXT)) ORDER BY e.id DESC LIMIT 30").fetchall()]
    for e in waiting:
        try:
            q = quote(api,e)
            if abs(D(q['spread'])) <= paper.CLOSED_PCT:
                with paper.session(path) as db:
                    db.execute('INSERT INTO virtual_meta (name,value) VALUES (?,?) ON CONFLICT (name) DO NOTHING',(f"gapreset:{e['episode_id']}",'1'))
        except Exception:
            pass
    risk_quotes=[]
    for e in rows(path):
        try:
            q = quote(api,e)
            with paper.session(path) as db:
                ensure(db); lock(db)
                current = db.execute('SELECT * FROM virtual_state WHERE episode_id=?',(e['episode_id'],)).fetchone()
                if current['state'] != 'open':
                    continue
                db.execute('UPDATE virtual_state SET current_json=?,sampled_at=? WHERE episode_id=?',(json.dumps(q),q['at'],e['episode_id']))
                db.execute('DELETE FROM virtual_meta WHERE name=?',(f'quote_availability:{e["episode_id"]}',))
                spread = D(q['spread'])
                # Convergence is the same +/-0.10% band for every entry sign.
                entry = D(e['executable_spread_pct'])
                converged = abs(spread) <= paper.CLOSED_PCT
                if e['episode_id']==22:
                    import episode22_diagnostics
                    diagnostic_funding=db.execute('SELECT value FROM virtual_meta WHERE name=?',('funding_live:22',)).fetchone()
                    try:
                        diagnostic_funding=json.loads(diagnostic_funding['value']) if diagnostic_funding else {}
                    except (ValueError,TypeError):
                        diagnostic_funding={}
                    episode22_diagnostics.emit(e,q,'observation',funding=diagnostic_funding)
                update_metrics(db,e,q,converged)
                if converged:
                    if q['net_pnl'] is not None and number(q['net_pnl'])>=0:
                        close_db(db,e,q)
                        continue
                    reason='CONVERGED_BUT_NET_NEGATIVE' if q['net_pnl'] is not None else 'UNKNOWN_SETTLEMENT_PNL'
                    q['auto_close_blocked_reason']=reason
                    db.execute('UPDATE virtual_state SET current_json=? WHERE episode_id=?',(json.dumps(q),e['episode_id']))
                    db.execute("INSERT INTO virtual_meta (name,value) VALUES (?,?) ON CONFLICT (name) DO UPDATE SET value=excluded.value",(f'auto_close_reason:{e["episode_id"]}',reason))
                risk_quotes.append((e,q))
                previous = D(current['last_notice_spread'])
                if abs(spread-previous) >= D('1.5'):
                    event(db,f"move:{e['episode_id']}:{q['at']}",e['episode_id'],f"VIRTUAL #{e['episode_id']} {e['symbol']}\n{pnl_line(q)}")
                    db.execute('UPDATE virtual_state SET last_notice_spread=? WHERE episode_id=?',(str(spread),e['episode_id']))
                level = max(0,int((spread-entry)//5))
                for step in range(current['last_warning']+1,level+1):
                    buttons = [[{'text':'Рассмотреть дополнительный вход','callback_data':f"add:{e['episode_id']}:{step}"}]]
                    event(db,f"wide:{e['episode_id']}:{step}",e['episode_id'],f"🚨 VIRTUAL #{e['episode_id']} {e['symbol']}: расширение +{step*5} п.п. от входа\n{pnl_line(q)}\nАвтоусреднения нет. Можно рассмотреть дополнительный виртуальный вход.",buttons)
                if level > current['last_warning']:
                    db.execute('UPDATE virtual_state SET last_warning=?,last_notice_spread=? WHERE episode_id=?',(level,str(spread),e['episode_id']))
        except Exception as exc:
            if bxdiag.is_offline(exc):
                try:
                    with paper.session(path) as db:
                        record_bingx_offline(db,e['episode_id'],exc,'observation')
                except Exception as diagnostic_error:
                    logging.warning('Offline state persistence unavailable (%s)',type(diagnostic_error).__name__)
            bxdiag.emit(exc,e['episode_id'],e['symbol'],'observation')
            logging.warning('Virtual observation #%s unavailable (%s)',e['episode_id'],type(exc).__name__)
    funding_warnings(api,path)
    # Reuse funding already refreshed by the existing warning worker; no new GET.
    for e,q in risk_quotes:
        try:
            with paper.session(path) as db:
                ensure(db); lock(db)
                risk_alert(db,e,q)
        except Exception as exc:
            logging.warning('Virtual risk monitoring #%s unavailable (%s)',e['episode_id'],type(exc).__name__)
    dispatch(api,path)


def funding_snapshot(api, e, path=None):
    """Fresh schedule and entry-side executable quotes for up to $50 per leg."""
    started = time.monotonic()
    rate, next_at = api.basis.fresh_funding(api,e['futures_exchange'],e['symbol'])
    rate = number(rate)
    if not math.isfinite(next_at) or next_at <= time.time():
        raise ValueError('Next funding unavailable')
    # Safe metadata from the existing read-only refresh, also reused by risk alerts.
    with paper.session(path) as db:
        ensure(db); lock(db)
        db.execute("INSERT INTO virtual_meta (name,value) VALUES (?,?) ON CONFLICT (name) DO UPDATE SET value=excluded.value",
                   (f"funding_live:{e['episode_id']}",json.dumps({'at':time.time(),'rate':str(rate),'next_at':next_at})))
    remaining = next_at-time.time()
    if not 0 < remaining <= 1800:
        return None
    original = json.loads(e['cost_snapshot_json'])
    asks,_ = checked_book(api.orderbook(e['spot_exchange'],e['symbol']))
    _,bids = checked_book(api.basis.futures_book(api,e['futures_exchange'],e['symbol'],number(original['multiplier'],True)))
    quantity = min(D(50)/asks[0][0],D(50)/bids[0][0])
    spent = api.basis.spot_cost(asks,quantity,api)
    received = api.sell_for_usdt(bids,quantity)
    if spent is None or received is None or spent > 50 or received > 50 or time.monotonic()-started > 30:
        raise ValueError('Fresh $50 executable depth unavailable')
    spot, future = number(spent,True)/quantity,number(received,True)/quantity
    position_notional = api.sell_for_usdt(bids,number(e['quantity'],True))
    expected = number(position_notional,True)*rate if position_notional is not None else None
    return {'expected_funding':str(expected) if expected is not None else None, 'at':time.time(),'rate':str(rate),'next_at':next_at,
            'spot_ask':str(asks[0][0]),'future_bid':str(bids[0][0]),
            'spot_vwap':str(spot),'future_vwap':str(future),
            'raw_spread':str((bids[0][0]/asks[0][0]-1)*100),
            'spread':str((future/spot-1)*100)}


def funding_warnings(api,path=None):
    for e in rows(path):
        try:
            # Schedule is refreshed independently of exit P&L/fee availability.
            q = funding_snapshot(api,e,path)
            if q is None:
                continue
            # Notification only: fresh negative funding; admission/position rules stay unchanged.
            if number(q['rate']) >= 0 or not 0 <= time.time()-q['at'] <= 30:
                continue
            key = f"funding:{e['episode_id']}:{round(q['next_at']*1000)}"
            try:
                pnl = quote(api,e)
                if pnl['net_pnl'] is None:
                    pnl_text = 'Net P&L сейчас: неизвестен; ' + pnl_line(pnl)
                else:
                    capital = number(e['used_capital'],True)
                    net = number(pnl['net_pnl'])
                    pnl_text = f'Net P&L сейчас: {net:+.4f} USDT ({net/capital*100:+.4f}% от виртуального капитала episode)'
            except Exception:
                pnl_text = 'Net P&L сейчас: недоступен — свежие цены закрытия или комиссии не подтверждены'
            remaining = int(q['next_at']-time.time())
            if not 0 < remaining <= 1800 or not 0 <= time.time()-q['at'] <= 30:
                continue
            body = (f"⚠️ До funding осталось {remaining//60} мин {remaining%60} сек\nEpisode #{e['episode_id']}\n"
                    f"Монета: {e['symbol'][:-4]}/USDT\nSpot: {e['spot_exchange']}\nFutures: {e['futures_exchange']}\n"
                    f"Входной спред по стакану: {D(e['executable_spread_pct']):+.4f}%\n"
                    f"Текущий спред Ask/Bid: {D(q['raw_spread']):+.4f}%; по стакану: {D(q['spread']):+.4f}%\n"
                    f"Spot Ask: {q['spot_ask']}; Futures Bid: {q['future_bid']}\n"
                    f"Исполнимые цены по стакану до $50 на ногу: Spot {q['spot_vwap']}; Futures {q['future_vwap']}\n"
                    + pnl_text + '\n'
                    + f"Funding: {D(q['rate'])*100:+.4f}%\n"
                    f"Следующий funding: {time.strftime('%Y-%m-%d %H:%M:%S UTC',time.gmtime(q['next_at']))}\n"
                    f"Осталось: {remaining//60} мин {remaining%60} сек\n"
                    + (f"Если оставить сделку открытой до funding, ожидаемый funding: {D(q['expected_funding']):+.6f} USDT\n" if q.get('expected_funding') is not None else "Ожидаемый funding: неизвестен\n")
                    + "Оценка по текущему исполнимому Futures Bid и ставке; фактическое начисление зависит от mark price и ставки в момент funding.\n"
                    "Решение принимает пользователь.\n⏳ Виртуальная сделка всё ещё открыта.\n🟡 PAPER / VIRTUAL ONLY")
            # Commit the unique claim BEFORE HTTP so restart cannot resend it.
            with paper.session(path) as db:
                ensure(db);lock(db)
                current=db.execute('SELECT state FROM virtual_state WHERE episode_id=?',(e['episode_id'],)).fetchone()
                if not current or current['state']!='open':
                    continue
                result=db.execute("INSERT INTO virtual_events (event_key,episode_id,body,state) VALUES (?,?,?,'claimed') ON CONFLICT (event_key) DO NOTHING",(key,e['episode_id'],body))
                if result.rowcount!=1:
                    continue
            with paper.session(path) as db:
                ensure(db);lock(db)
                current=db.execute('SELECT state FROM virtual_state WHERE episode_id=?',(e['episode_id'],)).fetchone()
                # Lock spans Telegram HTTP: a concurrent close cannot race this send.
                if not current or current['state']!='open' or not 0 <= time.time()-q['at'] <= 30 or not 0 < q['next_at']-time.time() <= 1800:
                    db.execute("UPDATE virtual_events SET state='cancelled' WHERE event_key=?",(key,))
                    continue
                api.telegram(body)
                db.execute("UPDATE virtual_events SET state='sent' WHERE event_key=?",(key,))
                db.execute('UPDATE virtual_state SET last_notice_spread=? WHERE episode_id=?',(q['spread'],e['episode_id']))
            logging.info('Virtual funding warning sent: episode #%s, funding event %s',e['episode_id'],int(q['next_at']))
        except Exception as exc:
            bxdiag.emit(exc,e['episode_id'],e['symbol'],'funding_warning')
            logging.warning('Virtual funding warning #%s unavailable or delivery uncertain (%s)',e['episode_id'],type(exc).__name__)


def dispatch(api,path=None):
    # At-most-once delivery: claim before HTTP. Uncertain sends are not retried.
    with paper.session(path) as db:
        ensure(db); lock(db)
        pending = db.execute("SELECT * FROM virtual_events WHERE state='pending' ORDER BY event_key LIMIT 30").fetchall()
        for row in pending:
            db.execute("UPDATE virtual_events SET state='claimed' WHERE event_key=?",(row['event_key'],))
    for row in pending:
        try:
            if row['event_key'].startswith('risk:'):
                # Only risk events: a concurrent close must cancel, not send, the alert.
                with paper.session(path) as db:
                    ensure(db); lock(db)
                    current=db.execute('SELECT state FROM virtual_state WHERE episode_id=?',(row['episode_id'],)).fetchone()
                    if not current or current['state']!='open':
                        db.execute("UPDATE virtual_events SET state='cancelled' WHERE event_key=?",(row['event_key'],))
                        continue
                    api.telegram(row['body'])
                    db.execute("UPDATE virtual_events SET state='sent' WHERE event_key=?",(row['event_key'],))
                continue
            api.telegram(row['body'],reply_markup={'inline_keyboard':json.loads(row['buttons_json'])} if row['buttons_json'] else None)
            with paper.session(path) as db:
                db.execute("UPDATE virtual_events SET state='sent' WHERE event_key=?",(row['event_key'],))
        except Exception as exc:
            logging.warning('Virtual notification delivery uncertain (%s)',type(exc).__name__)


@binance_io.fresh
def verified_entry(api,e):
    started = time.monotonic()
    context = {'symbol':e['symbol'], 'spot':e['spot_exchange'], 'future':e['futures_exchange']}
    try:
        item = api.basis.evaluate(api,e['symbol'],e['spot_exchange'],e['futures_exchange'])
    except Exception as exc:
        diagnostics.verification_snapshot('REVALIDATED_CALCULATION',dict(context,
            entry_calculation=getattr(exc,'entry_calculation',{})), 'PRIMARY', type(exc).__name__)
        raise
    diagnostics.verification_snapshot('REVALIDATED_CALCULATION',item or context,'PRIMARY')
    if not item or not api.basis.qualifies(item):
        diagnostics.verification_rejected(item,'PRIMARY')
        raise ValueError('Entry filters failed')
    anomaly = D(item['executable_spread_pct']) > 5 or D(item['raw_spread_pct']) > 5
    if anomaly:
        try:
            repeated = api.basis.evaluate(api,e['symbol'],e['spot_exchange'],e['futures_exchange'])
        except Exception as exc:
            diagnostics.verification_snapshot('REVALIDATED_CALCULATION',dict(context,
                entry_calculation=getattr(exc,'entry_calculation',{})), 'ANOMALY_REPEAT', type(exc).__name__)
            raise
        diagnostics.verification_snapshot('REVALIDATED_CALCULATION',repeated or context,'ANOMALY_REPEAT')
        if not repeated or not api.basis.qualifies(repeated) or (repeated['executable_spread_pct'] <= 5 and repeated['raw_spread_pct'] <= 5) or abs(repeated['executable_spread_pct']-item['executable_spread_pct']) > D('0.20') or abs(repeated['raw_spread_pct']-item['raw_spread_pct']) > D('0.20'):
            diagnostics.verification_rejected(repeated,'ANOMALY_REPEAT')
            raise ValueError('Anomalous spread not confirmed')
        item = repeated
        item['anomaly'] = 'ANOMALOUS_SPREAD_RECONFIRMED'
    tracing = diagnostics.CYCLE.get() is not None
    if tracing: diagnostics.flow('FINAL_FUNDING_RECHECK_ATTEMPT',item)
    try:
        rate,next_at = api.basis.fresh_funding(api,item['future'],item['symbol'])
    except Exception as exc:
        if tracing: diagnostics.flow('FINAL_FUNDING_RECHECK_FAILED',item,reason='RECHECK_EXCEPTION',exception_type=type(exc).__name__)
        raise
    item['funding'],item['next_funding_at'] = rate,next_at
    if tracing:
        if rate <= 0: diagnostics.flow('FINAL_FUNDING_RECHECK_FAILED',item,reason='NONPOSITIVE_FUNDING')
        else: diagnostics.flow('FINAL_FUNDING_RECHECK_PASSED',item)
    if not api.basis.qualifies(item) or time.monotonic()-started > 30:
        raise ValueError('Fresh mandatory data unavailable')
    return item


def menu(api, action, chat_id,path=None):
    entries = rows(path)
    buttons=[]; lines=['PAPER / VIRTUAL ONLY — открытые episodes']
    for e in entries:
        try:
            q=quote(api,e)
            lines.append(f"#{e['episode_id']} {e['symbol']} {e['spot_exchange']}→{e['futures_exchange']}: {pnl_line(q)}")
        except Exception:
            lines.append(f"#{e['episode_id']} {e['symbol']}: свежий P&L недоступен")
        buttons.append([{'text':f"#{e['episode_id']} {e['symbol']}",'callback_data':f"{action}:{e['episode_id']}"}])
    with paper.session(path) as db:
        ensure(db); lines.append(f"Used capital {used(db):.4f} / {working_capital_limit():.2f} USDT")
    api.telegram('\n'.join(lines) if entries else lines[0]+'\nОткрытых сделок нет.',chat_id,reply_markup={'inline_keyboard':buttons} if buttons else None)


def preview(api,action,ident,chat,user,path=None,level=None):
    e=load(ident,path)
    if e is None:
        raise ValueError('Episode no longer open')
    if action=='add':
        with paper.session(path) as db:
            ensure(db)
            warning=db.execute('SELECT state FROM virtual_events WHERE event_key=?',(f'wide:{ident}:{level}',)).fetchone()
            existing=db.execute('SELECT 1 FROM virtual_meta WHERE name=?',(f'added:{ident}:{level}',)).fetchone()
        if not warning or existing:
            raise ValueError('Expansion level unavailable or already used')
        live=quote(api,e)
        if D(live['spread'])-D(e['executable_spread_pct']) < int(level)*5:
            raise ValueError('Expansion no longer present')
        item=verified_entry(api,e)
        with paper.session(path) as db:
            ensure(db)
            if not __import__('exchange_capital').admits(db, item):
                raise ValueError('50% capital limit')
        q={'spread':str(item['executable_spread_pct']), 'spot_entry':str(item['spot_entry']), 'future_entry':str(item['future_entry']), 'funding':str(item['funding']), 'level':level}
        text=f"Размеры ног: {item['spot_cost']:.4f} / {item['future_notional']:.4f} USDT; комиссии Spot/Futures {item['spot_fee']*100:.4f}% / {item['future_fee']*100:.4f}%; модель net {item['pct']:+.4f}%; защитный резерв 0.20%.\nДополнительный VIRTUAL BUY Spot {q['spot_entry']} + SHORT Futures {q['future_entry']}; funding {D(q['funding'])*100:+.4f}%; spread {D(q['spread']):+.4f}%."
    else:
        q=quote(api,e)
        text=f"Закрытие VIRTUAL #{ident}: Spot SELL {q['spot_exit']}; Futures BUY {q['future_exit']}; комиссии {q['fees']}\n{pnl_line(q)}"
    token=secrets.token_hex(12)
    with paper.session(path) as db:
        ensure(db)
        db.execute('INSERT INTO virtual_proposals (token,episode_id,action,chat_id,user_id,expires_at,quote_json) VALUES (?,?,?,?,?,?,?)',(token,ident,action,str(chat),str(user),time.time()+60,json.dumps(q)))
    api.telegram(text+'\nВторое подтверждение действительно 60 секунд. Реальных ордеров нет.',chat,reply_markup={'inline_keyboard':[[{'text':'Подтвердить VIRTUAL','callback_data':'confirm:'+token}]]})
    return token


def confirm(api,token,chat,user,path=None):
    with paper.session(path) as db:
        ensure(db)
        proposal=db.execute('SELECT * FROM virtual_proposals WHERE token=?',(token,)).fetchone()
    if not proposal or proposal['consumed'] or proposal['expires_at']<time.time() or proposal['chat_id']!=str(chat) or proposal['user_id']!=str(user):
        raise ValueError('Confirmation invalid or expired')
    e=load(proposal['episode_id'],path)
    if not e: raise ValueError('Episode no longer open')
    before=json.loads(proposal['quote_json'])
    if proposal['action']=='add':
        live=quote(api,e)
        if D(live['spread'])-D(e['executable_spread_pct']) < int(before['level'])*5:
            raise ValueError('Expansion no longer present')
        item=verified_entry(api,e)
        spread=item['executable_spread_pct']
    else:
        q=quote(api,e); spread=D(q['spread'])
    price_keys = ('spot_entry','future_entry') if proposal['action']=='add' else ('spot_exit','future_exit')
    fresh_prices = item if proposal['action']=='add' else q
    price_changed = any(abs(number(fresh_prices[k],True)/number(before[k],True)-1)>D('0.002') for k in price_keys)
    if price_changed or abs(spread-D(before['spread']))>D('0.20'):
        raise ValueError('Prices changed: request a new preview')
    with paper.session(path) as db:
        ensure(db); lock(db)
        if not getattr(db,'is_postgres',False): db.execute('BEGIN IMMEDIATE')
        if proposal['action']=='close' and not 0 <= time.time()-q['at'] <= 30:
            raise ValueError('Quote expired during confirmation')
        result=db.execute('UPDATE virtual_proposals SET consumed=1 WHERE token=? AND consumed=0 AND expires_at>=?',(token,time.time()))
        if result.rowcount!=1: raise ValueError('Already confirmed')
        current=db.execute('SELECT state FROM virtual_state WHERE episode_id=?',(e['episode_id'],)).fetchone()
        if current['state']!='open': raise ValueError('Episode no longer open')
        if proposal['action']=='add':
            key=f"added:{e['episode_id']}:{before['level']}"
            if db.execute('SELECT 1 FROM virtual_meta WHERE name=?',(key,)).fetchone(): raise ValueError('Already added')
            # Record uses its own transaction; reserve proposal atomically below via caller db.
            ident=paper.record(item,path, parent_id=e['episode_id'], connection=db)
            if ident is None: raise ValueError('Entry/budget rejected')
            db.execute('INSERT INTO virtual_meta (name,value) VALUES (?,?)',(key,str(ident)))
            item['used_capital']=capital(item['spot_cost'],item['future_notional'])
            event(db,f'entry:{ident}',ident,api.basis.format_alert(item)+f"\nДополнительный entry #{ident}, parent #{e['episode_id']}")
        else:
            close_db(db,e,q)
    dispatch(api,path)


def handle(api,update,chat_id,path=None):
    callback=update.get('callback_query')
    message=callback.get('message',{}) if callback else update.get('message',{})
    chat=message.get('chat',{}).get('id')
    user=(callback or message).get('from',{}).get('id')
    operator=os.getenv('TELEGRAM_OPERATOR_ID',str(chat_id) if str(chat_id).isdigit() else '')
    if str(chat)!=str(chat_id) or not operator or str(user)!=operator:
        return False
    text=callback.get('data','') if callback else message.get('text','').split('@')[0].split()[0] if message.get('text') else ''
    if text not in ('/open','/close','/status') and not text.startswith(('view:','close:','add:','confirm:')):
        return False
    try:
        if text == '/status':
            status(api,chat,path)
        elif text in ('/open','/close'):
            menu(api,'view' if text=='/open' else 'close',chat,path)
        elif text.startswith('confirm:'):
            confirm(api,text.split(':')[1],chat,user,path)
        else:
            parts=text.split(':'); action=parts[0]; ident=int(parts[1])
            if action=='view':
                e=load(ident,path)
                if not e: raise ValueError('Episode no longer open')
                api.telegram(f"VIRTUAL #{ident} {e['symbol']}\n{pnl_line(quote(api,e))}",chat,reply_markup={'inline_keyboard':[[{'text':'Рассчитать закрытие','callback_data':f'close:{ident}'}]]})
            else:
                preview(api,action,ident,chat,user,path,int(parts[2]) if action=='add' else None)
    except Exception as exc:
        logging.warning('Virtual command rejected (%s)',type(exc).__name__)
        api.telegram('VIRTUAL: действие не выполнено. Нужны свежие данные, действующее подтверждение и свободный лимит 50%. Повторите предварительный расчёт.',chat)
    return True


def cursor(path=None):
    with paper.session(path) as db:
        ensure(db)
        row=db.execute("SELECT value FROM virtual_meta WHERE name='telegram_offset'").fetchone()
        return int(row['value']) if row else 0


def claim_update(ident,path=None):
    with paper.session(path) as db:
        ensure(db); lock(db)
        db.execute("INSERT INTO virtual_meta (name,value) VALUES ('telegram_offset','0') ON CONFLICT (name) DO NOTHING")
        result=db.execute("UPDATE virtual_meta SET value=? WHERE name='telegram_offset' AND CAST(value AS REAL)<?",(str(ident+1),ident+1))
        return result.rowcount==1
