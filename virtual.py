"""Persistent PAPER lifecycle. Exchange calls are read-only; no order API exists here."""
import json
import logging
import math
import os
import secrets
import time
from decimal import Decimal
import paper

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
CREATE TABLE IF NOT EXISTS virtual_meta (name TEXT PRIMARY KEY, value TEXT NOT NULL);
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
    value = D(os.getenv('PAPER_DEPOSIT_USDT', '500'))
    if not value.is_finite() or value <= 0:
        raise ValueError('Invalid virtual deposit')
    return value


def number(value, positive=False):
    value = D(str(value))
    if not value.is_finite() or (positive and value <= 0):
        raise ValueError('Unknown or invalid mandatory value')
    return value


def used(db):
    # Count both legs conservatively, including unresolved legacy episodes.
    rows = db.execute("SELECT e.*, v.state AS virtual_status FROM episodes e LEFT JOIN virtual_state v ON v.episode_id=e.id WHERE e.direction='spot_buy/futures_short'").fetchall()
    return sum((number(r['actual_spot_usdt'], True) + number(r['actual_futures_usdt'], True)
                for r in rows if r['virtual_status'] == 'open' or
                (r['virtual_status'] is None and r['first_close_min'] is None)), D(0))


def register(db, ident, item, parent=None):
    capital = number(item['spot_cost'], True) + number(item['future_notional'], True)
    db.execute('''INSERT INTO virtual_state (episode_id,parent_id,state,used_capital,last_notice_spread,anomaly)
                  VALUES (?,?,'open',?,?,?)''',
               (ident, parent, str(capital), str(item['executable_spread_pct']), item.get('anomaly')))


def bootstrap(path=None):
    with paper.session(path) as db:
        ensure(db); lock(db)
        active = db.execute("SELECT e.* FROM episodes e LEFT JOIN virtual_state v ON v.episode_id=e.id WHERE e.direction='spot_buy/futures_short' AND e.first_close_min IS NULL AND v.episode_id IS NULL").fetchall()
        for e in active:
            db.execute("INSERT INTO virtual_state (episode_id,state,used_capital,last_notice_spread) VALUES (?,'open',?,?) ON CONFLICT (episode_id) DO NOTHING", (e['id'],str(number(e['actual_spot_usdt'], True)+number(e['actual_futures_usdt'], True)),e['executable_spread_pct']))
        recovered = [dict(r) for r in db.execute("SELECT episode_id,last_notice_spread,last_warning FROM virtual_state WHERE state='open' ORDER BY episode_id").fetchall()]
        logging.info('Virtual recovery: %s open episodes; used capital %s / %s USDT; notification levels %s', len(recovered), used(db), deposit()/2, recovered)


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
    fees = short_entry * entry_future_fee + spot_sale * spot_fee + cover * future_fee
    trading_net = spot_sale + short_entry - cover - entry_cost - fees
    funding_known = time.time() < float(episode['next_funding_at'])
    spread = (short / spot_buy - 1) * 100
    return {'at':time.time(), 'spread':str(spread), 'spot_bid':str(spot_bids[0][0]),
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


def close_db(db, e, q):
    result = db.execute("UPDATE virtual_state SET state='closed',closed_at=?,close_json=?,current_json=? WHERE episode_id=? AND state='open'",(q['at'],json.dumps(q),json.dumps(q),e['episode_id']))
    if result.rowcount != 1:
        return False
    db.execute("UPDATE episodes SET status='closed', first_close_min=COALESCE(first_close_min,?) WHERE id=?",(max(1,math.ceil((q['at']-e['started_at'])/60)), e['episode_id']))
    if D(q['spread']) <= paper.CLOSED_PCT and (D(e['executable_spread_pct']) > 0 or D(q['spread']) >= 0):
        db.execute('INSERT INTO virtual_meta (name,value) VALUES (?,?) ON CONFLICT (name) DO NOTHING',(f"gapreset:{e['episode_id']}",'1'))
    event(db, f"close:{e['episode_id']}",e['episode_id'], f"✅ VIRTUAL CLOSED #{e['episode_id']} {e['symbol']}\nSpot SELL {q['spot_exit']}; Futures BUY {q['future_exit']}\n{pnl_line(q)}\nPAPER ONLY")
    return True


def observe(api, path=None):
    # Manual closure of a continuing gap must not create a fresh entry next scan.
    with paper.session(path) as db:
        ensure(db)
        waiting = [dict(r) for r in db.execute("SELECT e.*,v.* FROM episodes e JOIN virtual_state v ON v.episode_id=e.id WHERE v.state='closed' AND NOT EXISTS (SELECT 1 FROM virtual_meta m WHERE m.name='gapreset:' || CAST(e.id AS TEXT)) ORDER BY e.id DESC LIMIT 30").fetchall()]
    for e in waiting:
        try:
            q = quote(api,e)
            if D(q['spread']) <= paper.CLOSED_PCT and (D(e['executable_spread_pct']) > 0 or D(q['spread']) >= 0):
                with paper.session(path) as db:
                    db.execute('INSERT INTO virtual_meta (name,value) VALUES (?,?) ON CONFLICT (name) DO NOTHING',(f"gapreset:{e['episode_id']}",'1'))
        except Exception:
            pass
    for e in rows(path):
        try:
            q = quote(api,e)
            with paper.session(path) as db:
                ensure(db); lock(db)
                current = db.execute('SELECT * FROM virtual_state WHERE episode_id=?',(e['episode_id'],)).fetchone()
                if current['state'] != 'open':
                    continue
                db.execute('UPDATE virtual_state SET current_json=?,sampled_at=? WHERE episode_id=?',(json.dumps(q),q['at'],e['episode_id']))
                spread = D(q['spread'])
                # Negative entries must first observe convergence towards zero;
                # entering at -1% is not already a closed trade.
                entry = D(e['executable_spread_pct'])
                converged = spread <= paper.CLOSED_PCT if entry > 0 else D(0) <= spread <= paper.CLOSED_PCT
                if converged:
                    close_db(db,e,q)
                    continue
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
            logging.warning('Virtual observation #%s unavailable (%s)',e['episode_id'],type(exc).__name__)
    funding_warnings(api,path)
    dispatch(api,path)


def funding_snapshot(api, e):
    """Fresh schedule and entry-side executable quotes for up to $50 per leg."""
    started = time.monotonic()
    rate, next_at = api.basis.fresh_funding(api,e['futures_exchange'],e['symbol'])
    rate = number(rate)
    if not math.isfinite(next_at) or next_at <= time.time():
        raise ValueError('Next funding unavailable')
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
            q = funding_snapshot(api,e)
            if q is None:
                continue
            key = f"funding:{e['episode_id']}:{round(q['next_at']*1000)}"
            try:
                pnl = quote(api,e)
                if pnl['net_pnl'] is None:
                    pnl_text = 'Net P&L сейчас: неизвестен; ' + pnl_line(pnl)
                else:
                    capital = number(e['used_capital'],True)
                    net = number(pnl['net_pnl'])
                    pnl_text = f'Net P&L сейчас: {net:+.4f} USDT ({net/capital*100:+.4f}% от капитала обеих ног)'
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
            api.telegram(row['body'],reply_markup={'inline_keyboard':json.loads(row['buttons_json'])} if row['buttons_json'] else None)
            with paper.session(path) as db:
                db.execute("UPDATE virtual_events SET state='sent' WHERE event_key=?",(row['event_key'],))
        except Exception as exc:
            logging.warning('Virtual notification delivery uncertain (%s)',type(exc).__name__)


def verified_entry(api,e):
    started = time.monotonic()
    item = api.basis.evaluate(api,e['symbol'],e['spot_exchange'],e['futures_exchange'])
    if not item or not api.basis.qualifies(item):
        raise ValueError('Entry filters failed')
    anomaly = D(item['executable_spread_pct']) > 5 or D(item['raw_spread_pct']) > 5
    if anomaly:
        repeated = api.basis.evaluate(api,e['symbol'],e['spot_exchange'],e['futures_exchange'])
        if not repeated or not api.basis.qualifies(repeated) or (repeated['executable_spread_pct'] <= 5 and repeated['raw_spread_pct'] <= 5) or abs(repeated['executable_spread_pct']-item['executable_spread_pct']) > D('0.20') or abs(repeated['raw_spread_pct']-item['raw_spread_pct']) > D('0.20'):
            raise ValueError('Anomalous spread not confirmed')
        item = repeated
        item['anomaly'] = 'ANOMALOUS_SPREAD_RECONFIRMED'
    rate,next_at = api.basis.fresh_funding(api,item['future'],item['symbol'])
    item['funding'],item['next_funding_at'] = rate,next_at
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
        ensure(db); lines.append(f"Used capital {used(db):.4f} / {deposit()/2:.2f} USDT (обе ноги)")
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
            if used(db)+item['spot_cost']+item['future_notional'] > deposit()/2:
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
            event(db,f'entry:{ident}',ident,f"VIRTUAL дополнительный entry #{ident}, parent #{e['episode_id']}; BUY Spot + SHORT Futures. Реальных ордеров нет.")
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
    if text not in ('/open','/close') and not text.startswith(('view:','close:','add:','confirm:')):
        return False
    try:
        if text in ('/open','/close'):
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
