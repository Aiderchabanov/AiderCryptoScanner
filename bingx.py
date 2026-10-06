"""Official BingX REST, GET-only allowlist. No order/transfer/withdrawal methods."""
import entry_diagnostics as diagnostics
import bingx_diagnostics as bxdiag
import hashlib
import hmac
import os
import re
import threading
import time
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
import requests

BASE_URL = 'https://open-api.bingx.com'
PATHS = {
    'spot_symbols': '/openApi/spot/v1/common/symbols',
    'spot_tickers': '/openApi/spot/v1/ticker/24hr',
    'spot_book': '/openApi/spot/v1/ticker/bookTicker',
    'spot_depth': '/openApi/spot/v1/market/depth',
    'spot_fee': '/openApi/spot/v1/user/commissionRate',
    'contracts': '/openApi/swap/v2/quote/contracts',
    'futures_tickers': '/openApi/swap/v2/quote/ticker',
    'futures_book': '/openApi/swap/v2/quote/bookTicker',
    'futures_depth': '/openApi/swap/v2/quote/depth',
    'funding': '/openApi/swap/v2/quote/premiumIndex',
    'futures_fee': '/openApi/swap/v2/user/commissionRate',
    'networks': '/openApi/wallets/v1/capital/config/getall',
}
PRIVATE = {'spot_fee', 'futures_fee', 'networks'}


class BingXUnavailable(RuntimeError):
    """Safe message: never attach request/response objects or signed URLs."""


@bxdiag.trace(BingXUnavailable)
def number(value, minimum=Decimal(0), strict=False):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < minimum or (strict and result == minimum):
            raise ValueError
        return result
    except (ValueError, InvalidOperation, TypeError):
        raise BingXUnavailable('BingX required numeric field unavailable') from None


@bxdiag.trace(BingXUnavailable)
def pair(symbol):
    if not re.fullmatch(r'[A-Z0-9]+USDT', symbol):
        raise BingXUnavailable('BingX USDT symbol unavailable')
    return symbol[:-4] + '-USDT'


def enabled_flag(value):
    return value is True or value == 'true'


class Client:
    def __init__(self):
        self.enabled = os.getenv('BINGX_ENABLED', 'false').lower() == 'true'
        self.key = os.getenv('BINGX_API_KEY', '')
        self.secret = os.getenv('BINGX_API_SECRET', '')
        # Conservative global limiter also satisfies each endpoint's 1/s cap.
        try:
            delay = float(os.getenv('BINGX_MIN_REQUEST_INTERVAL_SEC', '1.05'))
            self.interval = max(1.05, delay) if delay < float('inf') else 1.05
        except ValueError:
            self.interval = 1.05
        self._lock = threading.Lock()
        self._next_request = 0
        self._blocked_until = 0
        self._cache = {}
        self.last_success = {}

    @property
    def credentials_ready(self):
        return bool(self.key and self.secret)

    @bxdiag.trace(BingXUnavailable)
    def request(self, kind, params=None):
        if kind not in PATHS:
            raise BingXUnavailable('BingX endpoint not in GET-only allowlist')
        if not self.enabled:
            raise BingXUnavailable('BingX disabled')
        private = kind in PRIVATE
        if private and not self.credentials_ready:
            raise BingXUnavailable('BingX read-only credentials missing')
        business = dict(params or {})
        if any(k not in ('symbol', 'coin', 'limit') or not re.fullmatch(r'[A-Za-z0-9_.-]+', str(v))
               for k, v in business.items()):
            raise BingXUnavailable('BingX invalid request parameters')
        with self._lock:
            now = time.monotonic()
            if now < self._blocked_until:
                raise BingXUnavailable('BingX API cooldown active')
            wait = max(0, self._next_request - now)
            if wait:
                time.sleep(wait)
            self._next_request = time.monotonic() + self.interval
            business['timestamp'] = int(time.time() * 1000)
            business['recvWindow'] = 5000
            params = dict(sorted(business.items()))
            headers = {'X-SOURCE-KEY': 'BX-AI-SKILL'}
            # Public queries also signed when read credentials exist. No fallback hosts.
            if self.credentials_ready:
                canonical = '&'.join(f'{k}={v}' for k, v in params.items())
                params['signature'] = hmac.new(self.secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
                headers['X-BX-APIKEY'] = self.key
            try:
                bxdiag.update(timeout=False, cooldown_active=False)
                response = requests.get(BASE_URL + PATHS[kind], params=params, headers=headers,
                                        timeout=10, allow_redirects=False)
                bxdiag.response_context(response)
                if response.status_code != 200:
                    try:
                        bxdiag.response_context(response, response.json())
                    except Exception:
                        pass
                    if response.status_code in (401, 403, 418, 429):
                        retry = response.headers.get('Retry-After', '')
                        delay = 3600 if response.status_code in (401, 403, 418) else 60
                        try:
                            seconds = float(retry)
                        except (ValueError, TypeError):
                            try:
                                seconds = parsedate_to_datetime(retry).timestamp() - time.time()
                            except (ValueError, TypeError, OverflowError):
                                seconds = 0
                        self._blocked_until = time.monotonic() + max(delay, seconds)
                    raise BingXUnavailable(f'BingX {kind} HTTP {response.status_code}')
                try:
                    payload = response.json()
                except ValueError as exc:
                    bxdiag.update(json_error=type(exc).__name__)
                    raise
                bxdiag.response_context(response, payload)
                if not isinstance(payload, dict) or payload.get('code') != 0:
                    code = payload.get('code') if isinstance(payload, dict) else None
                    if code == 100410:
                        self._blocked_until = time.monotonic() + 60
                    elif code in (100001, 100004, 100412, 100413, 100419):
                        self._blocked_until = time.monotonic() + 3600
                    raise BingXUnavailable(f'BingX {kind} API rejected request')
                if payload.get('data') is None:
                    raise BingXUnavailable(f'BingX {kind} data unavailable')
                self.last_success[kind] = time.time()
                return payload['data']
            except (requests.RequestException, ValueError) as exc:
                if isinstance(exc, requests.RequestException):
                    bxdiag.update(timeout=isinstance(exc, requests.Timeout), transport_error=type(exc).__name__)
                raise BingXUnavailable(f'BingX {kind} transport/data unavailable') from None

    def cached(self, key, ttl, loader):
        old = self._cache.get(key)
        if old and time.monotonic() < old[0]:
            return old[1]
        result = loader()
        self._cache[key] = (time.monotonic() + ttl, result)
        return result

    @bxdiag.trace(BingXUnavailable)
    def spot_symbols(self):
        data = self.cached('spot_symbols', 30, lambda: self.request('spot_symbols'))
        if not isinstance(data, dict) or not isinstance(data.get('symbols'), list):
            raise BingXUnavailable('BingX spot symbols unavailable')
        return {r['symbol'].replace('-', ''): r for r in data['symbols']
                if isinstance(r, dict) and r.get('symbol', '').endswith('-USDT') and r.get('status') == 1}

    @bxdiag.trace(BingXUnavailable)
    def contracts(self):
        data = self.cached('contracts', 30, lambda: self.request('contracts'))
        if not isinstance(data, list):
            raise BingXUnavailable('BingX perpetual contracts unavailable')
        # Some official responses omit numeric status; both explicit API states
        # are still mandatory and do not imply zero fees or order minima.
        return {r['symbol'].replace('-', ''): r for r in data
                if isinstance(r, dict) and r.get('symbol', '').endswith('-USDT')
                and r.get('currency') == 'USDT' and r.get('status') in (None, 1)
                and enabled_flag(r.get('apiStateOpen')) and enabled_flag(r.get('apiStateClose'))}

    @bxdiag.trace(BingXUnavailable)
    def tickers(self, futures=False):
        kind = 'futures_tickers' if futures else 'spot_tickers'
        data = self.request(kind)
        rows = data if isinstance(data, list) else [data]
        allowed = self.contracts() if futures else self.spot_symbols()
        result = {}
        for row in rows:
            if not isinstance(row, dict):
                raise BingXUnavailable('BingX ticker malformed')
            symbol = row.get('symbol', '').replace('-', '')
            if symbol not in allowed:
                continue
            # Strictly bid/ask, never lastPrice or markPrice.
            try:
                ask = number(row.get('askPrice'), strict=True)
                bid = number(row.get('bidPrice'), strict=True)
            except BingXUnavailable:
                continue  # Exclude this symbol; never substitute lastPrice.
            if bid > ask:
                continue
            result[symbol] = {'askPrice': str(ask), 'bidPrice': str(bid)}
        return result

    @bxdiag.trace(BingXUnavailable)
    def book_ticker(self, symbol, futures=False):
        data = self.request('futures_book' if futures else 'spot_book', {'symbol': pair(symbol)})
        if futures and isinstance(data, dict) and isinstance(data.get('book_ticker'), dict):
            row = data['book_ticker']
            data = {'symbol': row.get('symbol'), 'askPrice': row.get('ask_price'), 'bidPrice': row.get('bid_price')}
        if isinstance(data, list):
            data = next((r for r in data if r.get('symbol') == pair(symbol)), None)
        if not isinstance(data, dict) or data.get('symbol') != pair(symbol):
            raise BingXUnavailable('BingX book ticker unavailable')
        return {'askPrice': number(data.get('askPrice'), strict=True),
                'bidPrice': number(data.get('bidPrice'), strict=True)}

    @bxdiag.trace(BingXUnavailable)
    def orderbook(self, symbol, futures=False):
        data = self.request('futures_depth' if futures else 'spot_depth', {'symbol': pair(symbol), 'limit': 100})
        if not isinstance(data, dict):
            raise BingXUnavailable('BingX orderbook unavailable')
        timestamp = number(data.get('T') if futures else data.get('ts'), strict=True)
        age = time.time() - float(timestamp / 1000)
        bxdiag.update(stale_data=age > 30 or age < -5)
        if age > 30 or age < -5:
            raise BingXUnavailable('BingX stale orderbook')
        books = []
        for side in ('asks', 'bids'):
            rows = data.get(side)
            if not isinstance(rows, list) or not rows:
                raise BingXUnavailable('BingX orderbook depth unavailable')
            levels = []
            for row in rows:
                if not isinstance(row, (list, tuple)) or len(row) != 2:
                    raise BingXUnavailable('BingX malformed depth level')
                levels.append([number(row[0], strict=True), number(row[1], strict=True)])
            prices = [r[0] for r in levels]
            if len(prices) != len(set(prices)):
                raise BingXUnavailable('BingX duplicate price levels')
            # Spot API snapshots return asks descending in live responses.
            # Normalize real levels before taking best prices or filling depth.
            levels.sort(key=lambda row: row[0], reverse=side == 'bids')
            books.append(levels)
        if books[1][0][0] > books[0][0][0]:
            raise BingXUnavailable('BingX crossed orderbook')
        return tuple(books)

    @bxdiag.trace(BingXUnavailable)
    def fee(self, symbol, futures=False):
        def load():
            data = self.request('futures_fee' if futures else 'spot_fee',
                                {} if futures else {'symbol': pair(symbol)})
            row = data.get('commission') if futures and isinstance(data, dict) else data
            if not isinstance(row, dict):
                raise BingXUnavailable('BingX personal trading fee unavailable')
            value = number(row.get('takerCommissionRate'))
            if value >= 1:
                raise BingXUnavailable('BingX invalid trading fee')
            return value
        return self.cached(('fee', futures, symbol), 60, load)

    @bxdiag.trace(BingXUnavailable)
    def spot_rules(self, symbol, side='buy'):
        row = self.spot_symbols().get(symbol)
        if not row or (row.get('apiStateBuy' if side == 'buy' else 'apiStateSell') is False):
            raise BingXUnavailable('BingX spot inactive')
        return {'min_qty': diagnostics.call('MISSING_MIN_QTY', number, row.get('minQty')), 'min_quote': diagnostics.call('MISSING_MIN_NOTIONAL', number, row.get('minNotional')),
                'max_qty': number(row.get('maxQty'), strict=True),
                'max_quote': number(row.get('maxNotional'), strict=True),
                'step': diagnostics.call('MISSING_STEP_SIZE', number, row.get('stepSize'), strict=True),
                'market_max_qty': None, 'market_max_quote': number(row.get('maxMarketNotional'), strict=True)
                if row.get('maxMarketNotional') is not None else None}

    @bxdiag.trace(BingXUnavailable)
    def futures_meta(self, symbol):
        row = self.contracts().get(symbol)
        if not row:
            raise BingXUnavailable('BingX perpetual inactive')
        precision = row.get('quantityPrecision')
        if type(precision) is not int or not 0 <= precision <= 18:
            raise BingXUnavailable('BingX quantity precision unavailable')
        return {'step': Decimal(1).scaleb(-precision),
                'min_qty': diagnostics.call('MISSING_MIN_QTY', number, row.get('tradeMinQuantity'), strict=True),
                'min_notional': diagnostics.call('MISSING_MIN_NOTIONAL', number, row.get('tradeMinUSDT')),
                'multiplier': Decimal(1), 'funding': None}

    @bxdiag.trace(BingXUnavailable)
    def fresh_funding(self, symbol):
        data = self.request('funding', {'symbol': pair(symbol)})
        if isinstance(data, list):
            data = next((r for r in data if r.get('symbol') == pair(symbol)), None)
        if not isinstance(data, dict) or data.get('symbol') != pair(symbol):
            raise BingXUnavailable('BingX funding unavailable')
        rate = number(data.get('lastFundingRate'), minimum=Decimal('-0.1'))
        next_at = float(diagnostics.call('MISSING_FUNDING_TIMESTAMP', number, data.get('nextFundingTime'), strict=True) / 1000)
        # Live premiumIndex responses omit `time`; updateTime is the last
        # settlement and is not the age of this freshly fetched response.
        stamp = float(number(data['time'], strict=True) / 1000) if data.get('time') is not None else time.time()
        if abs(rate) > Decimal('0.1') or next_at <= time.time() or abs(time.time()-stamp) > 30:
            raise BingXUnavailable('BingX current funding/settlement unavailable')
        return rate, next_at

    @bxdiag.trace(BingXUnavailable)
    def networks(self, coin):
        data = self.cached(('networks', coin), 30, lambda: self.request('networks', {'coin': coin}))
        if not isinstance(data, list):
            raise BingXUnavailable('BingX network data unavailable')
        row = next((r for r in data if r.get('coin') == coin), None)
        if not row or not isinstance(row.get('networkList'), list):
            raise BingXUnavailable('BingX networks unavailable')
        result = []
        for network in row['networkList']:
            if not isinstance(network, dict) or not network.get('network'):
                raise BingXUnavailable('BingX malformed network')
            result.append({'network': network['network'],
                           'depositEnable': network.get('depositEnable'),
                           'withdrawEnable': network.get('withdrawEnable'),
                           'withdrawFee': number(network['withdrawFee']) if network.get('withdrawFee') is not None else None,
                           'withdrawMin': number(network['withdrawMin']) if network.get('withdrawMin') is not None else None,
                           'withdrawMax': number(network['withdrawMax']) if network.get('withdrawMax') is not None else None,
                           'contractAddress': network.get('contractAddress'),
                           'depositFee': None, 'withdrawRate': None})
        return result  # Unknown optional fees remain None, never fabricated zeros.
