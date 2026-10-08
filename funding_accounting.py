"""Evidence-only PAPER funding. Never submits orders or substitutes estimates.

MEXC documents settleTime/rate, not settlement fair price. BingX documents
fundingTime/rate; its extra markPrice response field has unverified historical
semantics. Both adapters therefore deliberately fail the valuation proof gate.
"""
import json
import time
from decimal import Decimal

import bingx
import mexc
import paper

D = Decimal
SOURCES = {
    'BingX': 'https://open-api.bingx.com/openApi/swap/v2/quote/fundingRate',
    'MEXC': 'https://contract.mexc.com/api/v1/contract/funding_rate/history',
}


def decimal(value, positive=False):
    result = D(str(value))
    if not result.is_finite() or (positive and result <= 0):
        raise ValueError('Invalid funding evidence number')
    return result


def unknown(reason, records=()):
    return {'funding_status': 'UNKNOWN_SETTLEMENT_PNL',
            'funding_realized_usdt': None, 'funding_unknown_reason': reason,
            'funding_settlements_seen': len(records)}


def evaluate(episode, records, complete, as_of):
    """Pure reducer: positive rate credits SHORT; negative rate debits SHORT.

    `valuation_verified` must be set by a source adapter backed by an official
    settlement-price definition, never inferred from decimal places/OHLC/live
    rates. Current MEXC/BingX adapters cannot set it true.
    """
    first = float(decimal(episode['next_funding_at'], True))
    entered = float(decimal(episode['started_at'], True))
    now = float(decimal(as_of, True))
    if first <= entered:
        return unknown('INVALID_FIRST_SETTLEMENT_TIMESTAMP')
    if now < first:
        return {'funding_status': 'no settlement crossed',
                'funding_realized_usdt': '0', 'funding_unknown_reason': None,
                'funding_settlements_seen': 0}
    if episode['direction'] != 'spot_buy/futures_short':
        return unknown('UNSUPPORTED_POSITION_DIRECTION')
    rows = [r for r in records if entered < r['settlement_at'] <= now]
    if not complete or not rows or min(r['settlement_at'] for r in rows) != first:
        return unknown('INCOMPLETE_SETTLEMENT_HISTORY', rows)
    times = [r['settlement_at'] for r in rows]
    if len(set(times)) != len(times):
        return unknown('DUPLICATE_SETTLEMENT_EVIDENCE', rows)
    qty = decimal(episode['quantity'], True)  # Existing base-asset matched quantity.
    total = D(0)
    for row in rows:
        if row.get('source') != SOURCES.get(episode['futures_exchange']):
            return unknown('UNVERIFIED_HISTORY_SOURCE', rows)
        if row.get('symbol') != episode['symbol']:
            return unknown('SETTLEMENT_SYMBOL_MISMATCH', rows)
        if row.get('valuation_verified') is not True:
            return unknown('SETTLEMENT_VALUATION_UNVERIFIED', rows)
        rate = decimal(row['rate'])
        if abs(rate) > D('0.1'):
            return unknown('INVALID_HISTORICAL_RATE', rows)
        total += qty * decimal(row['settlement_price'], True) * rate
    return {'funding_status': 'CONFIRMED_PAPER_FUNDING',
            'funding_realized_usdt': str(total), 'funding_unknown_reason': None,
            'funding_settlements_seen': len(rows)}


def normalized(row, exchange, symbol):
    wire_symbol = bingx.pair(symbol) if exchange == 'BingX' else mexc.pair(symbol)
    if not isinstance(row, dict) or row.get('symbol') != wire_symbol:
        raise ValueError('Historical funding symbol mismatch')
    key = 'fundingTime' if exchange == 'BingX' else 'settleTime'
    stamp = decimal(row[key], True)
    if stamp != stamp.to_integral_value():
        raise ValueError('Historical funding timestamp invalid')
    rate = decimal(row['fundingRate'])
    if abs(rate) > D('0.1'):
        raise ValueError('Historical funding rate invalid')
    # Intentionally ignore an undocumented markPrice, not evidence of valuation.
    return {'symbol': symbol, 'settlement_at': float(stamp / 1000),
            'rate': str(rate.normalize()), 'source': SOURCES[exchange],
            'valuation_verified': False, 'settlement_price': None}


def history(api, episode, as_of):
    exchange, symbol = episode['futures_exchange'], episode['symbol']
    first_ms = int(decimal(episode['next_funding_at'], True) * 1000)
    end_ms = int(as_of * 1000)
    client = api.bingx if exchange == 'BingX' else api.mexc

    def fetch():
        result = []
        boundary = end_ms
        for page in range(1, 21):  # Bounded load; exhaustion means UNKNOWN.
            if exchange == 'BingX':
                batch = client.request('funding_history', {
                    'symbol': bingx.pair(symbol), 'startTime': first_ms,
                    'endTime': boundary, 'limit': 1000})
                if not isinstance(batch, list):
                    raise ValueError('Historical funding response invalid')
            else:
                payload = client.request('funding_history', {
                    'symbol': mexc.pair(symbol), 'page_num': page, 'page_size': 100})
                batch = payload['resultList']
                if not isinstance(batch, list) or payload['currentPage'] != page:
                    raise ValueError('Historical funding page invalid')
            clean = [normalized(r, exchange, symbol) for r in batch]
            result.extend(r for r in clean if first_ms <= r['settlement_at'] * 1000 <= end_ms)
            if not clean:
                return result, True, end_ms / 1000
            oldest = min(r['settlement_at'] for r in clean)
            if exchange == 'BingX':
                if any(not first_ms <= r['settlement_at'] * 1000 <= boundary for r in clean):
                    raise ValueError('Funding history outside requested range')
                if len(clean) < 1000:
                    return result, True, end_ms / 1000
                boundary = int(oldest * 1000) - 1
            elif oldest * 1000 <= first_ms or page >= payload['totalPage']:
                return result, True, end_ms / 1000
        return result, False, end_ms / 1000

    # Shared adapter cache/limiter; no new frequency in scanner or admission.
    return client.cached(('paper-funding-history-v1', symbol, first_ms), 300, fetch)


def persist(episode, records, complete, covered_at, path=None):
    """Append evidence once in existing metadata; never edit episodes/snapshots."""
    import virtual
    ident = int(episode.get('episode_id') or episode['id'])
    with paper.session(path) as db:
        virtual.ensure(db)
        virtual.lock(db)
        for row in records:
            key = f"paper_funding_v1:{ident}:{int(row['settlement_at'] * 1000)}"
            body = json.dumps(dict(row, quantity=str(episode['quantity'])), sort_keys=True)
            old = db.execute('SELECT value FROM virtual_meta WHERE name=?', (key,)).fetchone()
            if old and old['value'] != body:
                raise ValueError('Conflicting immutable funding evidence; audit required')
            db.execute('INSERT INTO virtual_meta (name,value) VALUES (?,?) ON CONFLICT (name) DO NOTHING', (key, body))
        # Append collection cursor, including incompleteness, without rewriting it.
        key = f'paper_funding_cursor_v1:{ident}:{int(covered_at * 1000)}'
        value = json.dumps({'covered_at': covered_at, 'complete': complete,
                            'source': SOURCES[episode['futures_exchange']]}, sort_keys=True)
        db.execute('INSERT INTO virtual_meta (name,value) VALUES (?,?) ON CONFLICT (name) DO NOTHING', (key, value))


def snapshot(api, episode, path=None, now=None):
    now = time.time() if now is None else now
    try:
        initial = evaluate(episode, [], False, now)
        if initial['funding_status'] == 'no settlement crossed':
            return initial
        if episode['futures_exchange'] not in SOURCES:
            return unknown('OFFICIAL_SETTLEMENT_PROVIDER_UNAVAILABLE')
        # Closed records are immutable; never collect beyond their lifetime.
        if episode.get('state') not in (None, 'open'):
            return unknown('CLOSED_RECORD_NOT_RECOMPUTED')
        records, complete, covered_at = history(api, episode, now)
        persist(episode, records, complete, covered_at, path)
        # Cache coverage is never treated as current completeness.
        return evaluate(episode, records, complete and covered_at >= now, now)
    except Exception:
        # Do not log exception payloads, signed URLs or credentials.
        return unknown('HISTORY_OR_EVIDENCE_STORAGE_UNAVAILABLE')
