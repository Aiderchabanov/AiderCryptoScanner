"""Shared Binance GET pacing and short-lived public snapshots; no trading APIs."""
import copy
import functools
import json
import sys
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
diagnostic_requests = deque()
spot_error_diagnostics = deque(maxlen=100)
weight_headers = {}
server_weights = {}
weight_limits = {}
# A safety policy, NOT an assumed exchange limit. Actual limits come from metadata.
UNKNOWN_WEIGHT_SAFETY_THRESHOLD = 1000
SERVER_WEIGHT_HEADROOM_PCT = 80
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
    record = request_fields(host, path, params)
    record.update(timestamp=utc_timestamp(), estimated_weight=weight(path, params))
    while diagnostic_requests and diagnostic_requests[0][0] <= now - 60:
        diagnostic_requests.popleft()
    diagnostic_requests.append((now, record))
    logging.info('Binance REST request sent: %s', json.dumps({**record, **endpoint_load(host, path, now), **split_counters(now)}, sort_keys=True))
    if now - last_report >= 60:
        logging.info('Binance REST rolling load: requests_last_60s=%s estimated_weight_last_60s=%s',
                     snapshot['requests'], snapshot['estimated_weight'])
        last_report = now


def metadata_observed(host, path, result):
    """Use only metadata returned by an existing normal exchangeInfo request."""
    if path not in ('/api/v3/exchangeInfo', '/fapi/v1/exchangeInfo') or not isinstance(result, dict):
        return
    limits = []
    rows = result.get('rateLimits')
    if not isinstance(rows, list):
        return
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = row.get('limit')
        if (row.get('rateLimitType') == 'REQUEST_WEIGHT' and
                row.get('interval') == 'MINUTE' and row.get('intervalNum') == 1 and
                isinstance(value, int) and not isinstance(value, bool) and value > 0):
            limits.append(value)
    if limits:
        weight_limits[host] = min(limits)


def server_weight_guard(host, path, params):
    """Fail before HTTP; per-host, independent of exchange Retry-After cooldowns."""
    now = time.monotonic()
    samples = server_weights.get(host, deque())
    while samples and samples[0][0] <= now - 60:
        samples.popleft()
    if not samples:
        return
    # Keep the high watermark for 60s: minute resets or an inaccurate ticker
    # header must not erase a high depth header immediately.
    used = max(item[1] for item in samples)
    official = weight_limits.get(host)
    threshold = (official * SERVER_WEIGHT_HEADROOM_PCT // 100
                 if official is not None else UNKNOWN_WEIGHT_SAFETY_THRESHOLD)
    blocked = used + weight(path, params) >= threshold
    snapshot = rolling_load(now)
    logging.log(logging.WARNING if blocked else logging.INFO,
                'Binance server weight guard: host=%s local_requests_last_60s=%s '
                'local_estimated_weight_last_60s=%s binance_server_used_weight_1m=%s '
                'official_weight_limit_1m=%s safety_threshold=%s '
                'server_weight_safety_pause=%s request_blocked_before_http=%s blocked_endpoint=%s reason=%s',
                host, snapshot['requests'], snapshot['estimated_weight'], used,
                official if official is not None else 'UNKNOWN', threshold,
                str(blocked).lower(), str(blocked).lower(), path if blocked else '-',
                'SERVER_WEIGHT_SAFETY_PAUSE' if blocked else '-')
    if blocked:
        raise RuntimeError('SERVER_WEIGHT_SAFETY_PAUSE')


def response_observed(host, path, response):
    # Only allow numeric Binance weight headers, never arbitrary header values.
    numeric = {}
    for name, value in response.headers.items():
        name = name.lower()
        if re.fullmatch(r'x-mbx-used-weight(?:-[0-9]+[smhd])?', name) and isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 12:
            numeric[name] = int(value)
    if numeric:
        weight_headers[host] = {**weight_headers.get(host, {}),
                               'at': time.monotonic(), 'values': numeric}
        one_minute = [numeric[name] for name in
                      ('x-mbx-used-weight', 'x-mbx-used-weight-1m', 'x-mbx-used-weight-60s')
                      if name in numeric]
        if one_minute:
            now = time.monotonic()
            samples = server_weights.setdefault(host, deque())
            while samples and samples[0][0] <= now - 60:
                samples.popleft()
            samples.append((now, max(one_minute)))
            weight_headers[host]['binance_server_used_weight_1m'] = max(one_minute)
            weight_headers[host]['timestamp'] = time.time()
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


# Diagnostic-only helpers: never alter pacing, weights, cache or cooldown.
def utc_timestamp(at=None):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() if at is None else at))


def request_component():
    frame = sys._getframe(1)
    fallback = 'UNKNOWN'
    try:
        for _ in range(32):
            if frame is None:
                break
            module = frame.f_globals.get('__name__', '').split('.')[-1]
            name = frame.f_code.co_name
            if module == 'risk_monitor':
                return 'risk_monitor'
            if module == 'paper' and name == 'poll':
                return 'checkpoint'
            if module == 'basis' and name == 'scan':
                return 'scanner'
            if module == 'virtual':
                if name == 'observe': return 'observation_recovery'
                if name == 'funding_warnings': return 'funding_warning'
                if name in ('preview', 'confirm', 'menu', 'status'): return 'telegram_read_only'
                fallback = 'virtual_quote'
            if module == 'app' and name == 'basis_loop': return 'scanner'
            frame = frame.f_back
        return fallback
    finally:
        del frame


def request_fields(host, path, params):
    params = params if isinstance(params, dict) else {}
    symbol = params.get('symbol')
    limit = params.get('limit')
    return {'host': host if host in HOSTS else 'UNKNOWN',
            'endpoint': path if re.fullmatch(r'/(?:api|fapi|sapi)/v[0-9]+/[A-Za-z/]+', path) else 'UNKNOWN',
            'component': request_component(),
            'symbol': symbol if isinstance(symbol, str) and re.fullmatch(r'[A-Z0-9]{1,32}', symbol) else 'UNKNOWN',
            'limit': int(limit) if not isinstance(limit, bool) and re.fullmatch(r'[0-9]{1,5}', str(limit)) else 'UNKNOWN'}


def endpoint_load(host, path, now=None):
    rolling_load(now)
    rows = [r for r in request_history if r[1] == host]
    return {'requests_last_60s_for_host': len(rows),
            'requests_last_60s_for_endpoint': sum(r[2] == path for r in rows),
            'estimated_weight_last_60s_for_host': sum(r[3] for r in rows)}


def split_counters(now=None):
    rolling_load(now)
    spot = [r for r in request_history if r[1] == 'api.binance.com']
    futures = [r for r in request_history if r[1] == 'fapi.binance.com']
    depth = sum(r[2] == '/api/v3/depth' for r in spot)
    info = sum(r[2] == '/api/v3/exchangeInfo' for r in spot)
    return {'spot_requests_last_60s': len(spot), 'spot_depth_requests_last_60s': depth,
            'spot_exchange_info_requests_last_60s': info, 'spot_other_requests_last_60s': len(spot)-depth-info,
            'futures_requests_last_60s': len(futures),
            'futures_depth_requests_last_60s': sum(r[2] == '/fapi/v1/depth' for r in futures),
            'futures_funding_requests_last_60s': sum(r[2] in ('/fapi/v1/premiumIndex', '/fapi/v1/fundingRate', '/fapi/v1/fundingInfo') for r in futures),
            'futures_commission_requests_last_60s': sum(r[2] == '/fapi/v1/commissionRate' for r in futures)}


def sanitized_error(response):
    # Only recognize fixed Binance messages/templates; arbitrary text may echo secrets.
    try:
        payload = response.json()
    except (ValueError, TypeError):
        payload = None
    if not isinstance(payload, dict): return 'UNKNOWN', 'UNKNOWN'
    code = payload.get('code')
    code = code if isinstance(code, int) and not isinstance(code, bool) and abs(code) < 10**9 else 'UNKNOWN'
    msg = payload.get('msg')
    if not isinstance(msg, str) or not msg: return code, 'UNKNOWN'
    if msg in ('Too many requests.', 'Too many requests', 'Way too much request weight used; IP banned.', 'Invalid API-key, IP, or permissions for action.'):
        return code, msg
    if code == -1003:
        banned = re.search(r'banned until ([0-9]{10,16})(?:[ .]|$)', msg)
        if banned: return code, 'Too many requests; IP [REDACTED] banned until ' + banned.group(1)
        if 'Too much request weight used' in msg or 'Too many requests' in msg:
            return code, 'Too many requests / request weight limit (variable details redacted)'
    return code, 'REDACTED_UNRECOGNIZED_MESSAGE'


def spot_error_observed(host, path, response, params, delay):
    if host != 'api.binance.com' or response.status_code not in (418, 429): return
    code, message = sanitized_error(response)
    numeric = []
    for name, value in response.headers.items():
        if name.lower() in ('x-mbx-used-weight', 'x-mbx-used-weight-1m', 'x-mbx-used-weight-60s') and isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 12:
            numeric.append(int(value))
    retry = parse_retry_after(response.headers.get('Retry-After'))
    record = {**request_fields(host, path, params), **endpoint_load(host, path), **split_counters(),
              'timestamp': utc_timestamp(), 'http_status': response.status_code,
              'binance_code': code, 'sanitized_binance_msg': message,
              'Retry-After': retry if retry is not None else 'UNKNOWN',
              'server_used_weight': max(numeric) if numeric else 'UNKNOWN',
              'cooldown_until': utc_timestamp(time.time() + delay)}
    spot_error_diagnostics.append(record)
    logging.warning('Binance Spot error diagnostic: %s', json.dumps(record, sort_keys=True))
