"""Opt-in, entry-versioned PAPER estimates; no exchange payment is implied."""
import json
import time
from decimal import Decimal

import funding_accounting as evidence
import bingx
import mexc

D = Decimal
METHOD = 'PAPER_FUNDING_ESTIMATED_V1_PREVIOUS_1M_MARK_CLOSE'
PRICE_SOURCES = {
    'MEXC': 'https://contract.mexc.com/api/v1/contract/kline/fair_price/{symbol}',
    'BingX': 'https://open-api.bingx.com/openApi/swap/v1/market/markPriceKlines',
    'Binance': 'https://fapi.binance.com/fapi/v1/markPriceKlines',
    'Gate': 'https://api.gateio.ws/api/v4/futures/usdt/candlesticks',
}
RATE_SOURCES = dict(evidence.SOURCES, Binance='https://fapi.binance.com/fapi/v1/fundingRate',
                    Gate='https://api.gateio.ws/api/v4/futures/usdt/funding_rate')


def enabled(episode):
    try:
        entry = json.loads(episode['cost_snapshot_json']).get('paper_funding_model', {})
        return (entry.get('mode') == 'ESTIMATED' and entry.get('method') == METHOD
                and entry.get('activated_at') == episode['started_at'])
    except (KeyError, ValueError, TypeError):
        return False


def unknown(reason, count=0):
    return dict(funding_status='UNKNOWN', funding_realized_usdt=None,
                model_funding_pnl_usdt=None, funding_unknown_reason=reason,
                funding_settlements_seen=count, model_valid_until=None)


def closing_net(episode, quote):
    if not enabled(episode):
        value = quote.get('net_pnl')
        return evidence.decimal(value) if value is not None else None
    if quote.get('funding_mode') != 'ESTIMATED' or quote.get('funding_status') != 'ESTIMATED':
        return None
    try:
        if time.time() >= float(quote['model_valid_until']):
            return None  # Quote crossed another settlement boundary.
        value = evidence.decimal(quote['model_net_pnl_usdt'])
        expected = (evidence.decimal(quote['spot_pnl']) + evidence.decimal(quote['futures_pnl'])
                    + evidence.decimal(quote['model_funding_pnl_usdt']))
        return value if value == expected else None
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None


def model_price(api, exchange, symbol, settlement):
    """Close of exact [T-60s,T) historical mark/fair candle; no substitutes."""
    if settlement % 60:
        raise ValueError('Settlement is not minute-aligned')
    start = int(settlement) - 60
    if exchange == 'MEXC':
        data = api.mexc.request('funding_model_candles', dict(symbol=mexc.pair(symbol), interval='Min1', start=start, end=int(settlement)))
        rows = [(evidence.decimal(t), data['close'][i]) for i,t in enumerate(data['time'])]
    elif exchange == 'BingX':
        data = api.bingx.request('funding_model_candles', dict(symbol=bingx.pair(symbol), interval='1m', startTime=start*1000, endTime=int(settlement)*1000, limit=3))
        rows = [(evidence.decimal(r['openTime']) / 1000, r['close']) for r in data
                if int(r['closeTime']) in (int(settlement)*1000, int(settlement)*1000-1)]
    elif exchange == 'Binance':
        data = api.get_json(PRICE_SOURCES[exchange], params=dict(symbol=symbol, interval='1m', startTime=start*1000, endTime=int(settlement)*1000-1, limit=2))
        rows = [(evidence.decimal(r[0]) / 1000, r[4]) for r in data if int(r[6]) == int(settlement)*1000-1]
    elif exchange == 'Gate':
        data = api.gate('/futures/usdt/candlesticks', params={'contract':'mark_'+symbol[:-4]+'_USDT', 'interval':'1m', 'from':start, 'to':int(settlement)-1})
        rows = [(evidence.decimal(r['t']), r['c']) for r in data]
    else:
        raise ValueError('Model provider unavailable')
    exact = [evidence.decimal(p, True) for t,p in rows if t == start]
    if len(exact) != 1:
        raise ValueError('Exact historical candle unavailable')
    source = PRICE_SOURCES[exchange].format(symbol=mexc.pair(symbol)) if exchange == 'MEXC' else PRICE_SOURCES[exchange]
    return str(exact[0]), source, start


def history(api, episode, as_of):
    exchange = episode['futures_exchange']
    if exchange in evidence.SOURCES:
        # Reuse existing collector and request pacing. Cached coverage cannot be
        # extended beyond its actual query timestamp.
        return evidence.history(api, episode, as_of)
    first = int(episode['next_funding_at']); end = int(as_of)
    result = []; boundary = end
    for _ in range(20):
        if exchange == 'Binance':
            batch = api.get_json(RATE_SOURCES[exchange], params={'symbol':episode['symbol'], 'startTime':first*1000, 'endTime':boundary*1000, 'limit':1000})
            clean = [dict(symbol=r['symbol'], settlement_at=int(r['fundingTime'])/1000,
                          rate=str(evidence.decimal(r['fundingRate'])), source=RATE_SOURCES[exchange]) for r in batch]
            # Binance returns ascending records. Page forwards, not backwards.
            if len(clean) == 1000:
                next_first = int(max(r['settlement_at'] for r in clean)) + 1
            else: next_first = None
        elif exchange == 'Gate':
            batch = api.gate('/futures/usdt/funding_rate', params={'contract':episode['symbol'][:-4]+'_USDT', 'from':first, 'to':boundary, 'limit':1000})
            clean = [dict(symbol=episode['symbol'], settlement_at=int(r['t']),
                          rate=str(evidence.decimal(r['r'])), source=RATE_SOURCES[exchange]) for r in batch]
            next_first = None
        else: raise ValueError('Model provider unavailable')
        if any(r['symbol'] != episode['symbol'] or not first <= r['settlement_at'] <= boundary or abs(evidence.decimal(r['rate'])) > D('.1') for r in clean):
            raise ValueError('Invalid historical funding range')
        result.extend(clean)
        if len(clean) < 1000:
            return result, True, as_of
        if exchange == 'Binance': first = next_first
        else: boundary = int(min(r['settlement_at'] for r in clean)) - 1
    return result, False, as_of


def save(db, episode, row, received):
    ident = int(episode.get('episode_id') or episode['id'])
    stamp = int(row['settlement_at'] * 1000)
    old = db.execute('SELECT status,payload_json FROM paper_funding_model_events WHERE episode_id=? AND settlement_ms=?', (ident,stamp)).fetchone()
    if old and old['status'] == 'ESTIMATED':
        prior = json.loads(old['payload_json'])
        # Freeze completed model calculation, never revalue with a new price.
        for key in ('rate','quantity','source','model_price','method'):
            if prior.get(key) != row.get(key): raise ValueError('Model evidence conflict')
        return prior
    payload = dict(row, episode_id=ident, exchange=episode['futures_exchange'],
                   received_at=received, method=METHOD)
    if old:
        prior = json.loads(old['payload_json'])
        if prior.get('rate') is not None and row.get('rate') is not None and prior['rate'] != row['rate']:
            raise ValueError('Historical rate changed; audit required')
        if row['status'] == 'UNKNOWN' and all(prior.get(k) == row.get(k) for k in ('rate','quantity','source','unknown_reason')):
            return prior  # Repeated failure must not grow nested audit payloads.
        payload['enrichment_reason'] = 'OFFICIAL_DATA_RECOVERED'
        payload['previous_unknown'] = prior
        db.execute('UPDATE paper_funding_model_events SET status=?,payload_json=?,received_at=? WHERE episode_id=? AND settlement_ms=? AND status=?',
                   (row['status'],json.dumps(payload),received,ident,stamp,'UNKNOWN'))
    else:
        db.execute('INSERT INTO paper_funding_model_events VALUES (?,?,?,?,?)',
                   (ident,stamp,row['status'],json.dumps(payload),received))
    return payload


def snapshot(api, episode, path=None, now=None):
    import paper, virtual
    now = time.time() if now is None else now
    if not enabled(episode): return unknown('MODE_NOT_ACTIVATED_AT_ENTRY')
    if episode.get('direction') != 'spot_buy/futures_short': return unknown('UNSUPPORTED_POSITION_DIRECTION')
    try:
        entered = float(episode['started_at']); first = float(episode['next_funding_at'])
        if not entered < first: return unknown('INVALID_FIRST_SETTLEMENT')
        if now < first:
            return dict(funding_status='ESTIMATED', funding_realized_usdt=None,
                        model_funding_pnl_usdt='0', funding_unknown_reason=None,
                        funding_settlements_seen=0, model_valid_until=first)
        if episode.get('state') not in (None,'open'): return unknown('CLOSED_RECORD_NOT_RECOMPUTED')
        # Refresh next scheduled timestamp; never derive a fixed 8h schedule.
        _, next_at = api.basis.fresh_funding(api, episode['futures_exchange'], episode['symbol'])
        if not next_at > now: return unknown('NEXT_FUNDING_UNAVAILABLE')
        ident = int(episode.get('episode_id') or episode['id'])
        with paper.session(path) as db:
            virtual.ensure(db); virtual.lock(db)
            for stamp in (first, next_at):
                key = f'paper_model_expected:{ident}:{int(stamp*1000)}'
                db.execute('INSERT INTO virtual_meta VALUES (?,?) ON CONFLICT (name) DO NOTHING', (key,str(stamp)))
            expected = [float(r['value']) for r in db.execute('SELECT value FROM virtual_meta WHERE name LIKE ?', (f'paper_model_expected:{ident}:%',)).fetchall() if float(r['value']) <= now]
            # Every persisted receipt must remain in the complete history query.
            prior = db.execute('SELECT settlement_ms FROM paper_funding_model_events WHERE episode_id=?', (ident,)).fetchall()
            expected += [int(r['settlement_ms'])/1000 for r in prior]
            for stamp in expected:
                existing = db.execute('SELECT 1 FROM paper_funding_model_events WHERE episode_id=? AND settlement_ms=?', (ident,int(stamp*1000))).fetchone()
                if not existing:
                    save(db, episode, dict(symbol=episode['symbol'],settlement_at=stamp,
                        source=RATE_SOURCES[episode['futures_exchange']],rate=None,
                        quantity=str(episode['quantity']),method=METHOD,status='UNKNOWN',
                        model_price=None,model_price_source=None,model_amount_usdt=None,
                        unknown_reason='HISTORICAL_RATE_NOT_YET_AVAILABLE'),now)
        records, complete, covered = history(api, episode, now)
        rate_received = time.time()
        stamps = [r['settlement_at'] for r in records]
        if not complete or int(covered*1000) < int(now*1000) or len(stamps) != len(set(stamps)) or not set(expected).issubset(stamps):
            return unknown('INCOMPLETE_SETTLEMENT_HISTORY', len(records))
        if not records or min(stamps) != first: return unknown('FIRST_SETTLEMENT_MISSING')
        qty = evidence.decimal(episode['quantity'], True); total = D(0); failed = False
        for record in sorted(records, key=lambda r:r['settlement_at']):
            record = dict(record, rate=str(evidence.decimal(record['rate']).normalize()))
            if (record['symbol'] != episode['symbol'] or record['source'] != RATE_SOURCES[episode['futures_exchange']]
                    or not entered < record['settlement_at'] <= now):
                return unknown('INVALID_SETTLEMENT_EVIDENCE')
            row = dict(record, quantity=str(qty), method=METHOD, status='UNKNOWN', rate_received_at=rate_received, model_price=None,
                       model_price_source=None, model_amount_usdt=None)
            with paper.session(path) as db:
                old = db.execute('SELECT payload_json FROM paper_funding_model_events WHERE episode_id=? AND settlement_ms=? AND status=?', (ident,int(record['settlement_at']*1000),'ESTIMATED')).fetchone()
            if old:
                saved = json.loads(old['payload_json'])
                if any(saved.get(k) != row.get(k) for k in ('rate','quantity','source','method')):
                    return unknown('MODEL_EVIDENCE_CONFLICT')
                total += evidence.decimal(saved['model_amount_usdt']); continue
            try:
                price, source, start = model_price(api, episode['futures_exchange'], episode['symbol'], record['settlement_at'])
                rate = evidence.decimal(record['rate'])
                if abs(rate) > D('.1'): raise ValueError('Invalid rate')
                row.update(status='ESTIMATED', model_price=price, model_price_source=source,
                           candle_start_utc=start, candle_end_utc=record['settlement_at'],
                           model_price_received_at=time.time(),
                           model_amount_usdt=str(qty*evidence.decimal(price,True)*rate))
            except Exception:
                row['unknown_reason'] = 'HISTORICAL_MODEL_PRICE_OR_RATE_UNAVAILABLE'; failed = True
            with paper.session(path) as db:
                virtual.lock(db); save(db, episode, row, time.time())
            if row['status'] == 'ESTIMATED': total += evidence.decimal(row['model_amount_usdt'])
        if failed: return unknown('UNKNOWN_SETTLEMENT_IN_MODEL_LEDGER', len(records))
        return dict(funding_status='ESTIMATED', funding_realized_usdt=None,
                    model_funding_pnl_usdt=str(total), funding_unknown_reason=None,
                    funding_settlements_seen=len(records), model_valid_until=next_at)
    except Exception:
        return unknown('OFFICIAL_MODEL_DATA_OR_STORAGE_UNAVAILABLE')
