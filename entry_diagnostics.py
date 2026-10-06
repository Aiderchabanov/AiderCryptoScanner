"""Observation only: no HTTP, policy or execution decisions; existing JSON storage."""
import json
import logging
import re
import time
from collections import Counter
from contextvars import ContextVar
from decimal import Decimal, InvalidOperation

STAGE = ContextVar('entry_diagnostic_stage', default='OTHER_MANDATORY_DATA')
CYCLE = ContextVar('entry_diagnostic_cycle', default=None)
CALCULATION = ContextVar('entry_diagnostic_calculation', default=None)
NUMERIC_FIELDS = tuple('spot_ask spot_entry spot_quantity spot_cost spot_depth_usdt spot_fee spot_slippage_usdt future_bid future_entry quantity contracts future_notional futures_depth_usdt future_fee futures_slippage_usdt raw_spread_pct executable_spread_pct entry_fees_usdt slippage_usdt projected pct funding next_funding_at funding_age_seconds protective_reserve_usdt'.split())
STATUS_FIELDS = tuple('fee_known depth_known market_params_known funding_known data_fresh min_order_valid expected_net_known order_size_ok'.split())

def silent(reason, **values):
    """Annotate the existing return; never decide whether to return."""
    try:
        current=CALCULATION.get()
        if current is not None:
            current['silent_return_reason']=safe(reason)
            current.update({k:v for k,v in values.items() if k in STATUS_FIELDS})
    except Exception:
        pass

def flow(stage, item, reason=None, episode_id=None, exception_type=None):
    """Allowlisted lifecycle trace; no HTTP and no trading decision."""
    try:
        cycle=CYCLE.get()
        row=dict(scan_started_at=cycle['started_at'] if cycle else None,
                 timestamp=time.time(),stage=safe(stage),symbol=safe(item.get('symbol')),
                 spot_exchange=safe(item.get('spot')),futures_exchange=safe(item.get('future')),
                 direction=safe(item.get('spot'))+'->'+safe(item.get('future')),
                 reason=safe(reason) if reason is not None else None,
                 episode_id=episode_id if type(episode_id) is int else None,
                 exception_type=safe(exception_type) if exception_type else None,
                 entry_gate_passed=True if stage=='ENTRY_GATE_PASSED' else None)
        for key in ('pct','funding','next_funding_at','spot_cost','future_notional'):
            value=numeric(item.get(key));row[key]=None if value=='UNKNOWN' else value
        current=CALCULATION.get() or item.get('entry_calculation',{})
        row['silent_return_reason']=current.get('silent_return_reason')
        row['order_size_ok']=current.get('order_size_ok') if type(current.get('order_size_ok')) is bool else None
        logging.info('Entry flow diagnostic: %s',json.dumps(row,sort_keys=True))
        if cycle is not None:cycle.setdefault('flow_events',[]).append(row)
    except Exception:
        logging.warning('Entry flow diagnostic unavailable')

def capture(**values):
    """Copy allowlisted values already calculated by the entry path."""
    current = CALCULATION.get()
    if current is not None:
        current.update({k:v for k,v in values.items() if k in NUMERIC_FIELDS + STATUS_FIELDS})

def capture_book(venue, book):
    try:
        levels = book[0] if venue == 'spot' else book[1]
        parsed = [(Decimal(str(p)), Decimal(str(q))) for p,q in levels]
        depth = sum((p*q for p,q in parsed), Decimal(0)) if parsed and all(p.is_finite() and q.is_finite() and p>0 and q>0 for p,q in parsed) else None
        capture(**{'spot_depth_usdt' if venue == 'spot' else 'futures_depth_usdt':depth})
    except Exception:
        pass

def rejection_snapshot(code, item):
    current = CALCULATION.get() or {}
    saved = item.get('entry_calculation', {})
    values = {**current, **saved, **item}
    if saved.get('raw_spread_pct') is not None:
        values['raw_spread_pct'] = saved['raw_spread_pct']
    row = dict(timestamp=time.time(), symbol=safe(item.get('symbol')),
               spot_exchange=safe(item.get('spot')), futures_exchange=safe(item.get('future')),
               direction=safe(item.get('spot'))+'->'+safe(item.get('future')), reject_reason=safe(code))
    for key in NUMERIC_FIELDS:
        value = numeric(values.get(key))
        row[key] = None if value == 'UNKNOWN' else value
    for key in STATUS_FIELDS:
        value = values.get(key)
        row[key] = value if isinstance(value, bool) else None
    row['expected_net_known'] = row['pct'] is not None
    row['silent_return_reason']=safe(values['silent_return_reason']) if values.get('silent_return_reason') else None
    row['missing_mandatory_data'] = sorted(set(x for x in item.get('missing_mandatory_data', []) if x in CATEGORIES))
    missing = row['missing_mandatory_data']
    for key, categories in dict(fee_known=('MISSING_SPOT_FEE','MISSING_FUTURES_FEE'),
            depth_known=('MISSING_SPOT_DEPTH','MISSING_FUTURES_DEPTH'),
            market_params_known=('MISSING_SPOT_MARKET_PARAMS','MISSING_FUTURES_MARKET_PARAMS','MISSING_MIN_QTY','MISSING_STEP_SIZE','MISSING_MIN_NOTIONAL','MISSING_PRECISION'),
            funding_known=('MISSING_FUNDING','MISSING_FUNDING_TIMESTAMP'),
            data_fresh=('STALE_SPOT_DATA','STALE_FUTURES_DATA')).items():
        if any(x in missing for x in categories):row[key]=False
    cycle = CYCLE.get()
    row['scan_started_at'] = cycle['started_at'] if cycle is not None else None
    return row

def retain_rejection(row):
    current = CALCULATION.get()
    if current is not None:current['_rejection_recorded'] = True
    logging.info('Entry rejection calculation: %s', json.dumps(row, sort_keys=True))
    cycle = CYCLE.get()
    if cycle is not None:
        cycle['calculation_records']=cycle.get('calculation_records',0)+1
        if row.get('silent_return_reason'):
            cycle['silent_return_records']=cycle.get('silent_return_records',0)+1
    if cycle is not None and row['pct'] is not None:
        cycle['top_rejections'].append(row)
        cycle['top_rejections'].sort(key=lambda x: abs(Decimal(x['pct']) - Decimal('0.20')))
        del cycle['top_rejections'][10:]

def persist_rejection(db, row):
    """Reuse existing JSON-capable metadata storage; never create an episode."""
    key = 'entry_calculation:%s:%s:%s:%s' % (row['scan_started_at'] or row['timestamp'], row['direction'], row['symbol'], row['timestamp'])
    db.execute('SAVEPOINT entry_calculation_write')
    try:
        db.execute('INSERT INTO virtual_meta (name,value) VALUES (?,?)', (key, json.dumps(row, sort_keys=True)))
    except Exception:
        db.execute('ROLLBACK TO SAVEPOINT entry_calculation_write')
        raise
    finally:
        db.execute('RELEASE SAVEPOINT entry_calculation_write')

def observe_no_entry(item):
    """The existing evaluate path returned None without a named rejection.

    Keep its reason UNKNOWN; do not introduce a policy reason or counter.
    """
    try:
        row = rejection_snapshot('UNKNOWN',item)
        retain_rejection(row)
        import paper
        if paper.storage_ready():
            with paper.session() as db:
                persist_rejection(db,row)
    except Exception as exc:
        logging.warning('Entry calculation observation unavailable (%s)',type(exc).__name__)
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
        reasons = [x for x in item.get('mexc_spot_quantity_reasons', []) if x in
                   ('MEXC_SPOT_UNKNOWN_MIN_QTY','MEXC_SPOT_UNKNOWN_QUANTITY_INCREMENT','MEXC_SPOT_UNKNOWN_QUANTITY_RULES')]
        if item.get('spot') == 'MEXC' and reasons:
            row['mexc_spot_quantity_reasons'] = reasons
            counts['symbols_rejected_unknown_mexc_quantity'] += 1
            cycle['counts']['symbols_rejected_unknown_mexc_quantity'] += 1
        logging.info('Entry mandatory diagnostic: %s', json.dumps(row, sort_keys=True))
        cycle['recent_missing'].append(row)
        cycle['recent_missing'] = cycle['recent_missing'][-10:]

def begin():
    return CYCLE.set(dict(started_at=time.time(), counts=Counter(), directions={}, recent_missing=[], top_rejections=[]))

def candidates(shortlist):
    cycle = CYCLE.get()
    if cycle is not None:
        for _, _, spot, future, _ in shortlist:
            cycle['directions'].setdefault(spot+'->'+future, Counter())['candidates'] += 1

def completed(candidate_count, alert_count):
    cycle = CYCLE.get()
    if cycle is None:
        return
    keys = list(REJECTIONS.values()) + ['other_rejection','symbols_rejected_unknown_mexc_quantity'] + [x.lower() for x in CATEGORIES]
    def full(counts):
        result = {key: counts[key] for key in keys}
        result['candidates'] = counts['candidates']
        result['missing_market_params'] = counts['missing_spot_market_params'] + counts['missing_futures_market_params']
        result['missing_depth'] = counts['missing_spot_depth'] + counts['missing_futures_depth']
        result['stale_data'] = counts['stale_spot_data'] + counts['stale_futures_data']
        for key in ('scan_active','symbols_compared','symbols_fully_valid','conditional_alerts'):
            if key in counts: result[key] = counts[key]
        return result
    total = full(cycle['counts'])
    total['candidates'] = candidate_count
    logging.info('Entry rejection TOP10: %s', json.dumps(dict(scan_started_at=cycle['started_at'], top_rejections=cycle['top_rejections']), sort_keys=True))
    logging.info('Entry flow scan complete: %s',json.dumps(dict(scan_started_at=cycle['started_at'],
        calculation_records=cycle.get('calculation_records',0),silent_return_records=cycle.get('silent_return_records',0),
        stage_counts=dict(Counter(row['stage'] for row in cycle.get('flow_events',[])))),sort_keys=True))
    logging.info('Entry diagnostic scan complete: %s', json.dumps(dict(
        scan_started_at=cycle['started_at'], scan_completed_at=time.time(),
        candidates=candidate_count, conditional_alerts=alert_count, counts=total,
        directions={k:full(v) for k,v in cycle['directions'].items()},
        recent_missing=cycle['recent_missing']), sort_keys=True))
    try:
        import paper
        if paper.storage_ready():
            with paper.session() as db:
                db.execute('SAVEPOINT entry_flow_write')
                try:
                    db.execute('INSERT INTO virtual_meta (name,value) VALUES (?,?) ON CONFLICT (name) DO UPDATE SET value=excluded.value',
                               ('entry_flow:'+str(cycle['started_at']),json.dumps(cycle.get('flow_events',[]),sort_keys=True)))
                except Exception:
                    db.execute('ROLLBACK TO SAVEPOINT entry_flow_write')
                    raise
                finally:
                    db.execute('RELEASE SAVEPOINT entry_flow_write')
    except Exception as exc:
        logging.warning('Entry flow persistence unavailable (%s)',type(exc).__name__)
