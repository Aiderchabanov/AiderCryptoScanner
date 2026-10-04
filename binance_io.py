"""Shared Binance GET pacing and short-lived public snapshots; no trading APIs."""
import copy
import functools
import logging
import math
import threading
import time
from email.utils import parsedate_to_datetime

HOSTS = {'api.binance.com', 'fapi.binance.com'}
lock = threading.RLock()
local = threading.local()
next_request = 0.0
snapshots = {}
counts = {}
last_report = 0.0
last_clock = 0.0
PUBLIC_TTL = 1.0


def fresh(function):
    """Mandatory independent entry rechecks must never reuse a snapshot."""
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        previous = getattr(local, 'fresh', False)
        local.fresh = True
        try:
            return function(*args, **kwargs)
        finally:
            local.fresh = previous
    return wrapped


def retry_seconds(value):
    try:
        n = float(value)
        return max(0, n) if math.isfinite(n) else 0
    except (TypeError, ValueError):
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return 0


def key(host, path, params):
    if path not in ('/api/v3/depth', '/fapi/v1/depth', '/fapi/v1/premiumIndex'):
        return None
    return host, path, tuple(sorted((params or {}).items()))


def reuse(host, path, params):
    cache_key = key(host, path, params)
    old = snapshots.get(cache_key)
    if not getattr(local, 'fresh', False) and old and time.monotonic() < old[0]:
        return copy.deepcopy(old[1])
    return None


def weight(path, params):
    params = params or {}
    if path == '/api/v3/depth':
        limit = int(params.get('limit', 100))
        return 5 if limit <= 100 else 25 if limit <= 500 else 50 if limit <= 1000 else 250
    if path == '/fapi/v1/depth':
        limit = int(params.get('limit', 500))
        return 2 if limit <= 50 else 5 if limit <= 100 else 10 if limit <= 500 else 20
    if path.endswith('/ticker/bookTicker'):
        return 2 if params.get('symbol') else 4 if path.startswith('/api/') else 5
    if path == '/api/v3/exchangeInfo':
        return 20
    if path == '/fapi/v1/premiumIndex':
        return 1 if params.get('symbol') else 10
    # Conservative allowance for metadata and authenticated read-only endpoints.
    return 20 if path.startswith('/sapi/') or path == '/fapi/v1/commissionRate' else 1


def pace(host, path, params):
    global next_request, last_report, last_clock
    if time.monotonic() < last_clock:
        next_request = time.monotonic()
    wait = max(0, next_request - time.monotonic())
    if wait:
        time.sleep(wait)
    now = time.monotonic()
    last_clock = now
    request_weight = weight(path, params)
    # Combined ceiling 20 estimated weight/s, plus no overlapping Binance GETs.
    next_request = now + max(0.25, request_weight / 20)
    local.started = now
    item = counts.setdefault((host, path), [0, 0])
    item[0] += 1
    item[1] += request_weight
    if now - last_report >= 60:
        logging.info('Binance GET load: requests=%s estimated_weight=%s endpoints=%s',
                     sum(v[0] for v in counts.values()), sum(v[1] for v in counts.values()),
                     {h + p: v[0] for (h, p), v in counts.items()})
        counts.clear()
        last_report = now


def remember(host, path, params, result):
    cache_key = key(host, path, params)
    if cache_key is not None:
        deadline = getattr(local, 'started', time.monotonic()) + PUBLIC_TTL
        snapshots[cache_key] = deadline, copy.deepcopy(result)
