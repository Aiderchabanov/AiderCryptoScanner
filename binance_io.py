"""Shared Binance GET pacing and short-lived public snapshots; no trading APIs."""
import copy
import functools
import logging
import math
import re
from collections import deque
import threading
import time
from email.utils import parsedate_to_datetime

HOSTS = {'api.binance.com', 'fapi.binance.com'}
lock = threading.RLock()
local = threading.local()
next_request = 0.0
snapshots = {}
request_history = deque()
weight_headers = {}
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


def parse_retry_after(value):
    try:
        n = float(value)
        return max(0, n) if math.isfinite(n) else None
    except (TypeError, ValueError):
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return None


def retry_seconds(value):
    return parse_retry_after(value) or 0


def cooldown_seconds(status, retry_after):
    """Honor a supplied Binance deadline; defaults apply only if absent/invalid."""
    supplied = parse_retry_after(retry_after)
    if status in (418, 429):
        return supplied if supplied is not None else (3600 if status == 418 else 60)
    return max(3600, supplied or 0)


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
    global next_request, last_clock
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


def rolling_load(now=None):
    now = time.monotonic() if now is None else now
    while request_history and request_history[0][0] <= now - 60:
        request_history.popleft()
    return {'requests': len(request_history),
            'estimated_weight': sum(item[3] for item in request_history)}


def request_started(host, path, params):
    """Called immediately before a real GET, never for cache hits/cooldown."""
    global last_report
    now = time.monotonic()
    if request_history and now < request_history[-1][0]:
        request_history.clear()
    request_history.append((now, host, path, weight(path, params)))
    snapshot = rolling_load(now)
    if now - last_report >= 60:
        logging.info('Binance REST rolling load: requests_last_60s=%s estimated_weight_last_60s=%s',
                     snapshot['requests'], snapshot['estimated_weight'])
        last_report = now


def response_observed(host, path, response):
    # Only allow numeric Binance weight headers, never arbitrary header values.
    numeric = {}
    for name, value in response.headers.items():
        name = name.lower()
        if re.fullmatch(r'x-mbx-used-weight(?:-[0-9]+[smhd])?', name) and isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 12:
            numeric[name] = int(value)
    if numeric:
        weight_headers[host] = {'at': time.monotonic(), 'values': numeric}
    if response.status_code in (418, 429):
        snapshot = rolling_load()
        logging.warning('Binance REST rate limit: HTTP=%s requests_last_60s_before_429=%s estimated_weight_last_60s_before_429=%s binance_used_weight_header=%s endpoint_that_triggered_429=%s%s; counts include triggering GET; weight_header_reliable=%s',
                        response.status_code, snapshot['requests'], snapshot['estimated_weight'], numeric or 'unknown', host, path,
                        not (host == 'fapi.binance.com' and path == '/fapi/v1/ticker/bookTicker'))


def remember(host, path, params, result):
    cache_key = key(host, path, params)
    if cache_key is not None:
        deadline = getattr(local, 'started', time.monotonic()) + PUBLIC_TTL
        snapshots[cache_key] = deadline, copy.deepcopy(result)
