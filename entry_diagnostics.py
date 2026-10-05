"""Observation only: no HTTP, database writes, policy or execution decisions."""
import json
import logging
import re
import time
from collections import Counter
from contextvars import ContextVar
from decimal import Decimal, InvalidOperation

STAGE = ContextVar('entry_diagnostic_stage', default='OTHER_MANDATORY_DATA')
CYCLE = ContextVar('entry_diagnostic_cycle', default=None)
CATEGORIES = tuple('MISSING_SPOT_FEE MISSING_FUTURES_FEE MISSING_FUNDING MISSING_FUNDING_TIMESTAMP MISSING_SPOT_DEPTH MISSING_FUTURES_DEPTH STALE_SPOT_DATA STALE_FUTURES_DATA MISSING_SPOT_MARKET_PARAMS MISSING_FUTURES_MARKET_PARAMS MISSING_MIN_QTY MISSING_STEP_SIZE MISSING_MIN_NOTIONAL MISSING_PRECISION MISSING_EXECUTABLE_PRICE OTHER_MANDATORY_DATA'.split())
REJECTIONS = {'REJECTED_NON_POSITIVE_NET_ENTRY':'negative_net', 'REJECTED_LOW_EXPECTED_NET_RETURN':'expected_net_below_0_20', 'REJECTED_MIN_ORDER_ABOVE_TARGET':'min_order', 'REJECTED_NEGATIVE_FUNDING':'funding_nonpositive', 'REJECTED_UNKNOWN_FUNDING':'funding_unknown'}

def mark(stage):
    STAGE.set(stage)

def call(stage, function, *args, **kwargs):
    """Annotate the original exception; never retry or replace it."""
    token = STAGE.set(stage)
    try:
        return function(*args, **kwargs)
    except Exception as exc:
        if not getattr(exc, 'missing_mandatory_data', None):
            exc.missing_mandatory_data = classify(exc, stage)
        raise
    finally:
        STAGE.reset(token)

def classify(exc, stage=None):
    explicit = getattr(exc, 'missing_mandatory_data', None)
    if explicit:
        return sorted(set(x for x in explicit if x in CATEGORIES)) or ['OTHER_MANDATORY_DATA']
    stage = stage or STAGE.get()
    # Inspect messages only for category selection. Never emit exception text.
    message = str(exc).lower()
    if 'precision' in message:
        return ['MISSING_PRECISION']
    if 'minimum order filters' in message:
        return ['MISSING_MIN_QTY', 'MISSING_STEP_SIZE', 'MISSING_MIN_NOTIONAL']
    if ('stale' in message or 'freshness' in message) and stage in ('MISSING_SPOT_DEPTH','MISSING_FUTURES_DEPTH'):
        return ['STALE_SPOT_DATA' if stage == 'MISSING_SPOT_DEPTH' else 'STALE_FUTURES_DATA']
    return [stage if stage in CATEGORIES else 'OTHER_MANDATORY_DATA']

def safe(value):
    text = str(value)
    return text if re.fullmatch(r'[A-Za-z0-9_]+', text) else 'UNKNOWN'

def numeric(value):
    try:
        result = Decimal(str(value))
        return str(result) if result.is_finite() else 'UNKNOWN'
    except (InvalidOperation, TypeError, ValueError):
        return 'UNKNOWN'

def record(code, item):
    cycle = CYCLE.get()
    if cycle is None:
        return
    direction = safe(item.get('spot')) + '->' + safe(item.get('future'))
    counts = cycle['directions'].setdefault(direction, Counter())
    key = REJECTIONS.get(code, 'other_rejection')
    counts[key] += 1
    cycle['counts'][key] += 1
    missing = sorted(set(x for x in item.get('missing_mandatory_data', []) if x in CATEGORIES))
    if code == 'REJECTED_UNAVAILABLE_MANDATORY_DATA' and not missing:
        missing = ['OTHER_MANDATORY_DATA']
    for category in missing:
        key = category.lower()
        counts[key] += 1
        cycle['counts'][key] += 1
    if missing:
        row = dict(symbol=safe(item.get('symbol')), spot_exchange=safe(item.get('spot')),
                   futures_exchange=safe(item.get('future')), raw_spread_pct=numeric(item.get('raw_spread_pct')),
                   expected_net_pct=numeric(item.get('pct')), reject_reason=safe(code),
                   missing_mandatory_data=missing, diagnostic_scope='first_failure_no_extra_requests')
        logging.info('Entry mandatory diagnostic: %s', json.dumps(row, sort_keys=True))
        cycle['recent_missing'].append(row)
        cycle['recent_missing'] = cycle['recent_missing'][-10:]

def begin():
    return CYCLE.set(dict(started_at=time.time(), counts=Counter(), directions={}, recent_missing=[]))

def candidates(shortlist):
    cycle = CYCLE.get()
    if cycle is not None:
        for _, _, spot, future, _ in shortlist:
            cycle['directions'].setdefault(spot+'->'+future, Counter())['candidates'] += 1

def completed(candidate_count, alert_count):
    cycle = CYCLE.get()
    if cycle is None:
        return
    keys = list(REJECTIONS.values()) + ['other_rejection'] + [x.lower() for x in CATEGORIES]
    def full(counts):
        result = {key: counts[key] for key in keys}
        result['candidates'] = counts['candidates']
        result['missing_market_params'] = counts['missing_spot_market_params'] + counts['missing_futures_market_params']
        result['missing_depth'] = counts['missing_spot_depth'] + counts['missing_futures_depth']
        result['stale_data'] = counts['stale_spot_data'] + counts['stale_futures_data']
        return result
    total = full(cycle['counts'])
    total['candidates'] = candidate_count
    logging.info('Entry diagnostic scan complete: %s', json.dumps(dict(
        scan_started_at=cycle['started_at'], scan_completed_at=time.time(),
        candidates=candidate_count, conditional_alerts=alert_count, counts=total,
        directions={k:full(v) for k,v in cycle['directions'].items()},
        recent_missing=cycle['recent_missing']), sort_keys=True))
