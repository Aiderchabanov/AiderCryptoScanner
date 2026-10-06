"""Allowlisted error observations only. No HTTP, persistence or policy changes."""
import functools
import inspect
import json
import logging
import re
from contextvars import ContextVar

CONTEXT = ContextVar('bingx_error_context', default=None)
UNKNOWN = 'UNKNOWN'
FIELDS = tuple('exchange market operation endpoint_class symbol http_status api_code api_message api_message_safe availability_status timeout transport_error json_error missing_data stale_data cooldown_active exception_type reason'.split())
OPERATIONS = {'orderbook':'orderbook', 'fee':'fee', 'fresh_funding':'funding',
              'contracts':'market_metadata', 'spot_symbols':'market_metadata',
              'futures_meta':'market_metadata', 'spot_rules':'market_metadata',
              'tickers':'market_quote', 'book_ticker':'quote', 'networks':'networks',
              'pair':'symbol_mapping', 'number':'numeric_validation'}
ENDPOINTS = {'orderbook':('spot_depth','futures_depth'), 'fee':('spot_fee','futures_fee'),
             'book_ticker':('spot_book','futures_book'), 'tickers':('spot_tickers','futures_tickers'),
             'spot_symbols':('spot_symbols','spot_symbols'), 'contracts':('contracts','contracts'),
             'spot_rules':('spot_symbols','spot_symbols'), 'futures_meta':('contracts','contracts'),
             'fresh_funding':('funding','funding'), 'networks':('networks','networks')}

def safe_text(value, secrets=()):
    if not isinstance(value, str):
        return UNKNOWN
    for secret in secrets:
        if secret:
            value = value.replace(secret, '[REDACTED]')
    value = re.sub(r'https?://\S+', '[URL REDACTED]', value)
    value = re.sub(r'\b[^\s?&=]+=[^\s&]+(?:&[^\s&=]+=[^\s&]*)+', '[QUERY REDACTED]', value)
    value = re.sub(r'(?i)\b(?:signature|secret|api[_-]?key|authorization|token|cookie|timestamp|recvWindow)\b\s*[=:]\s*[^\s,;]+', '[REDACTED]', value)
    value = re.sub(r'\b[A-Fa-f0-9]{32,}\b', '[REDACTED]', value)
    return ' '.join(value.split())[:240] or UNKNOWN

def symbol_text(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Z0-9]{1,24}-?USDT', value):
        return UNKNOWN
    return value if value.endswith('-USDT') else value[:-4]+'-USDT'

def update(**values):
    current = CONTEXT.get()
    if current is not None:
        current.update({k:v for k,v in values.items() if k in FIELDS})

def response_context(response, payload=None):
    """Use the response already received. Never retain body, headers or URL."""
    current = CONTEXT.get()
    if current is None:
        return
    status = response.status_code
    current['http_status'] = status if type(status) is int else UNKNOWN
    if isinstance(payload, dict):
        code = payload.get('code')
        current['api_code'] = code if type(code) is int else UNKNOWN
        current['api_message'] = safe_text(payload.get('msg', payload.get('message')), current.get('_secrets', ()))
        current['api_message_safe'] = current['api_message']
        symbol = current.get('symbol', UNKNOWN)
        # Require an explicit statement about this exact symbol, not a generic error.
        if code != 0 and type(code) is int and symbol != UNKNOWN and re.search(
                r'(?<![A-Za-z0-9_-])'+re.escape(symbol)+r'\s+is\s+(?:offline\b|unavailable\b|not tradable\b)',
                current['api_message'], re.IGNORECASE):
            current.update(reason='BINGX_SYMBOL_OFFLINE', availability_status='OFFLINE')

def is_offline(exc):
    context = getattr(exc, 'diagnostic_context', {})
    return type(exc).__name__ == 'BingXUnavailable' and context.get('availability_status') == 'OFFLINE'

def trace(error_class):
    def decorate(function):
        signature = inspect.signature(function)
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            parent = CONTEXT.get()
            current = dict(parent) if parent else {key:UNKNOWN for key in FIELDS}
            client = args[0] if args and hasattr(args[0], '_blocked_until') else None
            try:
                bound = signature.bind(*args, **kwargs).arguments
                bound = {**bound, **bound.get('kwargs', {})}
                name = function.__name__
                if client:
                    current['_secrets'] = (client.key, client.secret)
                current['exchange'] = 'BingX'
                if name not in ('number','pair','request') or parent is None:
                    current['operation'] = OPERATIONS.get(name, name)
                if name == 'request':
                    kind = bound.get('kind')
                    # Only identifiers from the official adapter allowlist.
                    from bingx import PATHS
                    current['endpoint_class'] = kind if kind in PATHS else UNKNOWN
                    if parent is None: current['operation'] = 'request'
                    symbol = (bound.get('params') or {}).get('symbol')
                    if symbol is not None: current['symbol'] = symbol_text(symbol)
                elif name in ENDPOINTS:
                    futures = bound.get('futures', False) or name in ('contracts','futures_meta','fresh_funding')
                    current['endpoint_class'] = ENDPOINTS[name][bool(futures)]
                    current['market'] = 'Futures' if futures else 'Spot'
                if 'symbol' in bound: current['symbol'] = symbol_text(bound['symbol'])
                endpoint = current.get('endpoint_class')
                if name == 'request' and endpoint != UNKNOWN:
                    current['market'] = 'Futures' if endpoint.startswith('futures_') or endpoint in ('contracts','funding') else 'Spot' if endpoint.startswith('spot_') else UNKNOWN
                if name == 'pair' and parent is None: current['market'] = UNKNOWN
            except Exception:
                pass  # Diagnostics never alter the wrapped operation.
            token = CONTEXT.set(current)
            try:
                return function(*args, **kwargs)
            except error_class as exc:
                try:
                    saved = dict(getattr(exc, 'diagnostic_context', {}) or current)
                    saved['operation'] = current['operation']
                    saved['exception_type'] = type(exc).__name__
                    if saved.get('availability_status') != 'OFFLINE':
                        saved['reason'] = safe_text(str(exc), current.get('_secrets', ()))
                    if 'stale' in str(exc).lower(): saved['stale_data'] = True
                    if 'cooldown' in str(exc).lower(): saved['cooldown_active'] = True
                    if str(exc) in ('BingX required numeric field unavailable','BingX orderbook unavailable','BingX orderbook depth unavailable','BingX personal trading fee unavailable','BingX funding unavailable','BingX perpetual inactive','BingX spot inactive','BingX perpetual contracts unavailable','BingX spot symbols unavailable') or str(exc).endswith(' data unavailable'): saved['missing_data'] = True
                    exc.diagnostic_context = {k:saved.get(k,UNKNOWN) for k in FIELDS}
                except Exception:
                    pass
                raise
            finally:
                if parent is not None:
                    for key in ('http_status','api_code','api_message','timeout','transport_error','json_error'):
                        parent[key] = current.get(key,UNKNOWN)
                CONTEXT.reset(token)
        return wrapped
    return decorate

def emit(exc, episode_id, symbol, worker):
    if type(exc).__name__ != 'BingXUnavailable':
        return
    try:
        row = {k:getattr(exc,'diagnostic_context',{}).get(k,UNKNOWN) for k in FIELDS}
        row.update(episode_id=episode_id, symbol=symbol_text(symbol), worker=worker)
        logging.warning('BINGX_UNAVAILABLE: %s', json.dumps(row, sort_keys=True))
    except Exception:
        pass
