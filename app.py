import hashlib
import hmac
import logging
import os
import threading
import time
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from urllib.parse import urlencode, urlparse

import requests
from flask import Flask
import basis
import paper
import exchanges
import binance_io

BINANCE = 'https://api.binance.com'
GATE = 'https://api.gateio.ws/api/v4'
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
BINANCE_KEY = os.getenv('BINANCE_API_KEY', '')
BINANCE_SECRET = os.getenv('BINANCE_API_SECRET', '')
GATE_KEY = os.getenv('GATE_API_KEY', '')
GATE_SECRET = os.getenv('GATE_API_SECRET', '')
THRESH = max(Decimal('0.5'), Decimal(os.getenv('MIN_NET_PROFIT_PCT', '0.5')))
TRADE = Decimal('50')
RESERVE = Decimal('50')
PRICE_BUFFER_PCT = Decimal('0.20')
COOLDOWN = int(os.getenv('ALERT_COOLDOWN_SEC', '1800'))
SCAN = int(os.getenv('SCAN_INTERVAL_SEC', '20'))
MAX_CANDIDATES = int(os.getenv('MAX_CANDIDATES_PER_SCAN', '10'))
last_alert = {}
cache = {}
api_blocked_until = {}
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)


def dec(value):
    try:
        n = Decimal(str(value))
        return n if n.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def positive(value):
    n = dec(value)
    return n if n is not None and n >= 0 else None


def log_binance_commission_response(response):
    code=None;message='unrecognized response message redacted'
    allowed={
        'Invalid API-key, IP, or permissions for action.',
        'API-key format invalid.',
        'Signature for this request is not valid.',
        'Timestamp for this request is outside of the recvWindow.',
        "Timestamp for this request was 1000ms ahead of the server's time.",
        'Mandatory parameter \'symbol\' was not sent, was empty/null, or malformed.',
        'Invalid symbol.',
    }
    try:
        payload=response.json()
        if isinstance(payload,dict):
            value=payload.get('code')
            if isinstance(value,int) and not isinstance(value,bool):code=value
            raw=payload.get('msg')
            if raw in allowed:message=raw
    except (ValueError,TypeError):
        message='non-JSON response; body not logged'
    retry=response.headers.get('Retry-After','')
    safe_retry=retry if isinstance(retry,str) and retry.isdigit() and len(retry)<=12 else 'absent_or_non_numeric'
    logging.warning('Binance commission request failed: HTTP status=%s error code=%s message=%s Retry-After=%s',response.status_code,code if code is not None else 'unknown',message,safe_retry)


def get_json(url, **kwargs):
    if urlparse(url).netloc in binance_io.HOSTS:
        with binance_io.lock:
            return _get_json(url, **kwargs)
    return _get_json(url, **kwargs)


def _get_json(url, **kwargs):
    parsed = urlparse(url)
    host_key = parsed.netloc
    path_key = (parsed.netloc, parsed.path)
    now = time.monotonic()
    public_diagnostic = host_key == 'fapi.binance.com' and parsed.path in ('/fapi/v1/ticker/bookTicker','/fapi/v1/exchangeInfo')
    if host_key in binance_io.HOSTS and host_key not in api_blocked_until:
        try:
            until = paper.load_api_backoff(host_key)
            api_blocked_until[host_key] = max(now, now + until - time.time())
        except Exception:
            # Fail closed if the persisted Binance cooldown cannot be read.
            logging.warning('Binance backoff storage unavailable: host=%s', host_key)
            api_blocked_until[host_key] = now + 3600
    if now < max(api_blocked_until.get(host_key, 0), api_blocked_until.get(path_key, 0)):
        if public_diagnostic:
            remaining=max(api_blocked_until.get(host_key,0),api_blocked_until.get(path_key,0))-now
            until=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(time.time()+remaining))
            logging.warning('Binance futures public GET %s: HTTP=not_requested exception=RuntimeError message=active persisted/local cooldown; remaining=%.0fs until=%s; Retry-After=honored; no HTTP request sent',parsed.path,remaining,until)
        raise RuntimeError('Exchange API temporarily unavailable')
    if host_key in binance_io.HOSTS:
        reused = binance_io.reuse(host_key, parsed.path, kwargs.get('params'))
        if reused is not None:
            return reused
        binance_io.pace(host_key, parsed.path, kwargs.get('params'))
        # Pacing may delay a signed request: sign its actual parameters at dispatch.
        if kwargs.get('headers', {}).get('X-MBX-APIKEY') and BINANCE_SECRET:
            params = dict(kwargs.get('params') or {})
            params.pop('signature', None)
            params['timestamp'] = int(time.time() * 1000)
            params['signature'] = hmac.new(BINANCE_SECRET.encode(), urlencode(params).encode(), hashlib.sha256).hexdigest()
            kwargs['params'] = params
    try:
        response = requests.get(url, timeout=10, **kwargs)
    except requests.RequestException as exc:
        if public_diagnostic:
            label='timeout' if isinstance(exc,requests.Timeout) else 'transport failure'
            logging.warning('Binance futures public GET %s: HTTP=no_response exception=%s message=%s Retry-After=unavailable',parsed.path,type(exc).__name__,label)
        raise
    if public_diagnostic:
        retry=response.headers.get('Retry-After','')
        safe_retry=retry if isinstance(retry,str) and retry.isdigit() and len(retry)<=12 else 'absent_or_non_numeric'
        code=None
        if response.status_code>=400:
            try:
                payload=response.json()
                candidate=payload.get('code') if isinstance(payload,dict) else None
                if isinstance(candidate,int):code=candidate
            except (ValueError,TypeError):pass
        label={418:'IP auto-ban',429:'rate limit',451:'restricted location',403:'access forbidden',401:'unauthorized'}.get(response.status_code,'HTTP error' if response.status_code>=400 else 'response received')
        logging.log(logging.WARNING if response.status_code>=400 else logging.INFO,
            'Binance futures public GET %s: HTTP=%s exception=%s message=%s API_code=%s Retry-After=%s',
            parsed.path,response.status_code,'HTTPError' if response.status_code>=400 else 'none',label,code if code is not None else '-',safe_retry)
    if host_key == 'fapi.binance.com' and parsed.path == '/fapi/v1/commissionRate' and response.status_code >= 400:
        log_binance_commission_response(response)
    if response.status_code in (401, 403, 418, 429):
        retry = response.headers.get('Retry-After', '')
        base_delay = 3600 if response.status_code in (401, 403, 418) else 60
        delay = max(base_delay, binance_io.retry_seconds(retry)) if host_key in binance_io.HOSTS else (max(base_delay, int(retry)) if retry.isdigit() else base_delay)
        block_key = host_key if response.status_code in (418, 429) else path_key
        api_blocked_until[block_key] = time.monotonic() + delay
        if host_key in binance_io.HOSTS and response.status_code in (418, 429):
            try:
                paper.save_api_backoff(host_key, time.time() + delay)
            except Exception:
                logging.warning('Binance backoff could not be persisted: host=%s', host_key)
        error_label = ''
        if host_key == 'api.gateio.ws' and response.status_code == 401:
            try:
                label = response.json().get('label', '')
                if (isinstance(label, str) and label.isascii() and
                        label.replace('_', '').isalnum() and len(label) <= 64):
                    error_label = f' label={label}'
            except (ValueError, AttributeError):
                pass
        logging.warning('Exchange API HTTP %s%s at %s%s; paused %ss',
                        response.status_code, error_label, parsed.netloc, parsed.path, delay)
    response.raise_for_status()
    result = response.json()
    if host_key in binance_io.HOSTS:
        binance_io.remember(host_key, parsed.path, kwargs.get('params'), result)
    return result


def cached(key, ttl, loader):
    if isinstance(key, tuple) and 'Binance' in key:
        with binance_io.lock:
            return _cached(key, ttl, loader)
    return _cached(key, ttl, loader)


def _cached(key, ttl, loader):
    old = cache.get(key)
    if old and time.monotonic() < old[0]:
        return old[1]
    value = loader()
    cache[key] = (time.monotonic() + ttl, value)
    return value


def binance(path, params=None, signed=False):
    params = dict(params or {})
    headers = {}
    if signed:
        if not BINANCE_KEY or not BINANCE_SECRET:
            raise RuntimeError('Binance read-only credentials missing')
        params['timestamp'] = int(time.time() * 1000)
        params['recvWindow'] = 5000
        query = urlencode(params)
        params['signature'] = hmac.new(BINANCE_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
        headers['X-MBX-APIKEY'] = BINANCE_KEY
    payload = get_json(BINANCE + path, params=params, headers=headers)
    if isinstance(payload, dict) and 'code' in payload and int(payload['code']) < 0:
        raise RuntimeError('Binance API rejected request')
    return payload


def binance_signed_params(params):
    if not BINANCE_KEY or not BINANCE_SECRET:
        raise RuntimeError('Binance read-only credentials missing')
    params = dict(params)
    params['timestamp'] = int(time.time() * 1000)
    params['recvWindow'] = 5000
    params['signature'] = hmac.new(BINANCE_SECRET.encode(), urlencode(params).encode(),
                                   hashlib.sha256).hexdigest()
    return params



def gate(path, params=None, signed=False):
    query = '&'.join(f'{key}={value}' for key, value in (params or {}).items())
    headers = {}
    if signed:
        if not GATE_KEY or not GATE_SECRET:
            raise RuntimeError('Gate read-only credentials missing')
        timestamp = str(int(time.time()))
        body_hash = hashlib.sha512(b'').hexdigest()
        message = '\n'.join(('GET', '/api/v4' + path, query, body_hash, timestamp))
        signature = hmac.new(GATE_SECRET.encode(), message.encode(), hashlib.sha512).hexdigest()
        headers = {'KEY': GATE_KEY, 'Timestamp': timestamp, 'SIGN': signature}
    payload = get_json(GATE + path, params=params, headers=headers)
    if isinstance(payload, dict) and payload.get('label'):
        raise RuntimeError('Gate API rejected request')
    return payload


def tickers():
    b = binance('/api/v3/ticker/bookTicker')
    g = gate('/spot/tickers')
    if not isinstance(b, list) or not isinstance(g, list):
        raise RuntimeError('Invalid market ticker data')
    return ({x['symbol']: x for x in b if x.get('symbol', '').endswith('USDT')},
            {x['currency_pair'][:-5] + 'USDT': x for x in g
             if x.get('currency_pair', '').endswith('_USDT')})


def telegram(msg, chat_id=None, reply_markup=None):
    if not TOKEN or not (chat_id or CHAT_ID):
        return
    response = requests.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage',
                             json={'chat_id': chat_id or CHAT_ID, 'text': msg, **({'reply_markup':reply_markup} if reply_markup else {})}, timeout=10)
    if response.status_code != 200 or not response.json().get('ok'):
        raise RuntimeError(f'Telegram sendMessage HTTP {response.status_code}')


def fee(exchange, symbol):
    def load():
        if exchange == 'Binance':
            rows = binance('/sapi/v1/asset/tradeFee', {'symbol': symbol}, True)
            row = next((x for x in rows if x.get('symbol') == symbol), None)
            value = row and positive(row.get('takerCommission'))
        else:
            pair = symbol[:-4] + '_USDT'
            row = gate('/wallet/fee', {'currency_pair': pair}, True)
            value = positive(row.get('taker_fee')) if isinstance(row, dict) else None
        if value is None or value >= 1:
            raise RuntimeError('Trading fee unavailable')
        return value
    return cached(('fee', exchange, symbol), 60, load)


def networks(exchange, coin):
    if exchange == 'Gate':
        def load_gate():
            chains = gate('/wallet/currency_chains', {'currency': coin})
            statuses = gate('/wallet/withdraw_status', {'currency': coin}, True)
            if not isinstance(chains, list) or not isinstance(statuses, list):
                raise RuntimeError('Gate network data unavailable')
            status = next((s for s in statuses if s.get('currency', '').upper() == coin), None)
            if not status:
                return []
            fees = status.get('withdraw_fix_on_chains') or {}
            rates = status.get('withdraw_percent_on_chains') or {}
            if not isinstance(fees, dict) or not isinstance(rates, dict):
                raise RuntimeError('Gate network fees unavailable')
            result = []
            for chain in chains:
                name = chain.get('chain')
                if not name:
                    continue
                # A global fixed fee is unambiguous only for a single chain.
                fixed = fees.get(name, status.get('withdraw_fix') if len(chains) == 1 else None)
                percent = rates.get(name, status.get('withdraw_percent'))
                result.append({'network': name,
                               'withdrawEnable': chain.get('is_disabled') == 0 and chain.get('is_withdraw_disabled') == 0,
                               'depositEnable': chain.get('is_disabled') == 0 and chain.get('is_deposit_disabled') == 0,
                               'withdrawFee': fixed, 'withdrawRate': percent,
                               'withdrawMin': status.get('withdraw_amount_mini'),
                               'withdrawMax': status.get('withdraw_eachtime_limit'),
                               'depositFee': status.get('deposit'),
                               'contractAddress': chain.get('contract_address')})
            return result
        return cached(('chains', 'Gate', coin), 30, load_gate)
    def load_all():
        rows = binance('/sapi/v1/capital/config/getall', signed=True)
        if not isinstance(rows, list):
            raise RuntimeError('Binance chain data unavailable')
        return {x['coin']: x.get('networkList', []) for x in rows if 'coin' in x}
    return cached(('chains', 'Binance'), 30, load_all).get(coin, [])


def spot_rules(exchange, symbol, side):
    """Read market-order limits; missing or malformed critical fields fail closed."""
    def optional_limit(row, key):
        raw = row.get(key)
        if raw is None:
            return None
        value = positive(raw)
        if value is None:
            raise RuntimeError('Malformed pair limit')
        return value

    def load():
        if exchange == 'Gate':
            row = gate('/spot/currency_pairs/' + symbol[:-4] + '_USDT')
            if not isinstance(row, dict) or row.get('id') != symbol[:-4] + '_USDT':
                raise RuntimeError('Gate pair rules unavailable')
            if not {'min_base_amount', 'min_quote_amount', 'amount_precision', 'trade_status'} <= row.keys():
                raise RuntimeError('Gate pair limits unavailable')
            if row.get('trade_status') not in ('tradable', 'buyable' if side == 'buy' else 'sellable'):
                raise RuntimeError('Gate pair not open for this side')
            precision = row.get('amount_precision')
            if type(precision) is not int or not 0 <= precision <= 18:
                raise RuntimeError('Gate amount precision unavailable')
            return {'min_qty': optional_limit(row, 'min_base_amount') or Decimal(0),
                    'min_quote': optional_limit(row, 'min_quote_amount') or Decimal(0),
                    'max_qty': optional_limit(row, 'max_base_amount'),
                    'max_quote': optional_limit(row, 'max_quote_amount'),
                    'step': Decimal(1).scaleb(-precision),
                    'market_max_qty': optional_limit(row, 'market_order_max_stock'),
                    'market_max_quote': optional_limit(row, 'market_order_max_money')}
        rows = binance('/api/v3/exchangeInfo', {'symbol': symbol})
        row = next((x for x in rows.get('symbols', []) if x.get('symbol') == symbol), None)
        if not row or row.get('status') != 'TRADING' or 'MARKET' not in row.get('orderTypes', []):
            raise RuntimeError('Binance market pair unavailable')
        filters = {x['filterType']: x for x in row.get('filters', [])}
        lot = filters.get('LOT_SIZE')
        market = filters.get('MARKET_LOT_SIZE') or lot
        if not lot or not market:
            raise RuntimeError('Binance lot rules unavailable')
        market_step = positive(market.get('stepSize'))
        lot_step = positive(lot.get('stepSize'))
        step = market_step or lot_step
        min_qty = positive(market.get('minQty'))
        max_qty = positive(market.get('maxQty'))
        lot_min, lot_max = positive(lot.get('minQty')), positive(lot.get('maxQty'))
        if step is None or step <= 0 or not lot_step or min_qty is None or lot_min is None or max_qty is None or lot_max is None:
            raise RuntimeError('Binance lot rules malformed')
        if market_step and (max(step, lot_step) / min(step, lot_step)) % 1:
            raise RuntimeError('Binance market and lot steps incompatible')
        step = max(step, lot_step)
        limits = [x for x in (filters.get('MIN_NOTIONAL'), filters.get('NOTIONAL')) if x]
        if not limits:
            raise RuntimeError('Binance notional rule unavailable')
        min_values = [positive(x.get('minNotional')) for x in limits]
        if any(x is None for x in min_values):
            raise RuntimeError('Binance notional rule malformed')
        max_values = [positive(x.get('maxNotional')) for x in limits if x.get('maxNotional') is not None]
        if any(x is None for x in max_values):
            raise RuntimeError('Binance notional max malformed')
        max_quantities = [x for x in (max_qty, lot_max) if x > 0]
        return {'min_qty': max(min_qty, lot_min), 'min_quote': max(min_values),
                'max_qty': min(max_quantities) if max_quantities else None,
                'max_quote': min(max_values) if max_values else None, 'step': step,
                'market_max_qty': None, 'market_max_quote': None}
    # Trading status is checked on each scan. Structural limits can be cached briefly.
    return cached(('spot-rules', exchange, symbol, 'both' if exchange == 'Binance' else side), 30, load)


def order_size_ok(rules, quantity, notional):
    if not rules or quantity <= 0 or notional <= 0:
        return False
    for key in ('min_qty', 'min_quote', 'step'):
        if rules.get(key) is None:
            return False
    if quantity < rules['min_qty'] or notional < rules['min_quote']:
        return False
    for key, value in (('max_qty', quantity), ('market_max_qty', quantity),
                       ('max_quote', notional), ('market_max_quote', notional)):
        if rules.get(key) is not None and rules[key] > 0 and value > rules[key]:
            return False
    return True


def sale_quantity(quantity, rules):
    step = rules.get('step') if rules else None
    if not step or step <= 0:
        return None
    return (quantity / step).to_integral_value(rounding=ROUND_DOWN) * step


def chain_id(chain):
    raw = str(chain or '').upper().replace(' ', '').replace('-', '')
    aliases = {'ERC20': 'ETH', 'ETHEREUM': 'ETH', 'TRC20': 'TRX', 'TRON': 'TRX',
               'BEP20(BSC)': 'BSC', 'BEP20': 'BSC', 'BNBSMARTCHAIN': 'BSC',
               'SOLANA': 'SOL', 'MATIC': 'POLYGON', 'POLYGONPOS': 'POLYGON',
               'ARBITRUMONE': 'ARBITRUM', 'OP': 'OPTIMISM'}
    return aliases.get(raw, raw)


def chain_options(src, dst, source_exchange, amount):
    dest_exchange = 'Gate' if source_exchange == 'Binance' else 'Binance'
    def field(row, exchange, kind):
        if exchange in ('Binance', 'Gate'):
            return row.get({'id': 'network', 'withdraw': 'withdrawEnable',
                            'deposit': 'depositEnable', 'fee': 'withdrawFee',
                            'min': 'withdrawMin', 'max': 'withdrawMax',
                            'deposit_min': 'depositMin'}[kind])
        raise RuntimeError('Unknown exchange')
    found = []
    for a in src:
        name_a = chain_id(field(a, source_exchange, 'id'))
        if not name_a:
            continue
        if field(a, source_exchange, 'withdraw') is not True:
            continue
        fixed = positive(field(a, source_exchange, 'fee'))
        percent = a.get('withdrawRate') if source_exchange == 'Gate' else '0'
        if isinstance(percent, str) and percent.endswith('%'):
            pct_value = positive(percent[:-1])
            pct = pct_value / 100 if pct_value is not None else None
        else:
            pct = positive(percent)
        minimum = positive(field(a, source_exchange, 'min'))
        max_raw = field(a, source_exchange, 'max')
        maximum = positive(max_raw) if max_raw not in (None, '') else None
        if fixed is None or pct is None or pct >= 1 or minimum is None or amount < minimum or (maximum is not None and amount > maximum):
            continue
        for b in dst:
            name_b = chain_id(field(b, dest_exchange, 'id'))
            if field(b, dest_exchange, 'deposit') is not True or name_a != name_b:
                continue
            ca, cb = str(a.get('contractAddress') or a.get('contract') or '').lower(), str(b.get('contractAddress') or b.get('contract') or '').lower()
            if (ca or cb) and ca != cb:
                continue
            # Add variable and minimum fees conservatively when both apply.
            deposit_fee = positive(b.get('depositFee')) if dest_exchange == 'Gate' else Decimal(0)
            if deposit_fee is None:
                continue
            received = amount - fixed - amount * pct - deposit_fee
            deposit_min = positive(field(b, dest_exchange, 'deposit_min') or '0')
            if received > 0 and deposit_min is not None and received >= deposit_min:
                found.append((name_a, received, amount - received))
    return found


def orderbook(exchange, symbol):
    if exchange == 'Gate':
        pair = symbol[:-4] + '_USDT'
        result = gate('/spot/order_book', {'currency_pair': pair, 'limit': 500})
        return result.get('asks', []), result.get('bids', [])
    result = binance('/api/v3/depth', {'symbol': symbol, 'limit': 100})
    return result.get('asks', []), result.get('bids', [])


def buy_for_usdt(asks, budget):
    remaining, quantity = budget, Decimal(0)
    for raw_price, raw_qty in asks:
        price, qty = dec(raw_price), dec(raw_qty)
        if not price or not qty or price <= 0 or qty <= 0:
            return None
        take = min(qty, remaining / price)
        quantity += take
        remaining -= take * price
        if remaining <= Decimal('0.00000001'):
            return quantity
    return None


def sell_for_usdt(bids, quantity):
    remaining, received = quantity, Decimal(0)
    for raw_price, raw_qty in bids:
        price, qty = dec(raw_price), dec(raw_qty)
        if not price or not qty or price <= 0 or qty <= 0:
            return None
        take = min(qty, remaining)
        received += take * price
        remaining -= take
        if remaining <= Decimal('0.00000001'):
            return received
    return None


def estimate(symbol, buy, sell, top_ask, top_bid):
    coin = symbol[:-4]
    asks, _ = orderbook(buy, symbol)
    _, bids = orderbook(sell, symbol)
    if not asks or not bids:
        return None
    top_ask, top_bid = dec(asks[0][0]), dec(bids[0][0])
    if not top_ask or not top_bid or top_ask <= 0 or (top_bid / top_ask - 1) * 100 < THRESH:
        return None  # Recheck the actual orderbooks after the ticker shortlist.
    base = buy_for_usdt(asks, TRADE)
    if base is None:
        return None
    buy_rules = spot_rules(buy, symbol, 'buy')
    sell_rules = spot_rules(sell, symbol, 'sell')
    if not order_size_ok(buy_rules, base, TRADE):
        return None
    buy_rate, sell_rate = fee(buy, symbol), fee(sell, symbol)
    coin_to_transfer = base * (1 - buy_rate)
    coin_routes = chain_options(networks(buy, coin), networks(sell, coin), buy, coin_to_transfer)
    best = None
    for coin_net, transferred_qty, coin_cost in coin_routes:
        sell_qty = sale_quantity(transferred_qty, sell_rules)
        if sell_qty is None or sell_qty <= 0:
            continue
        proceeds = sell_for_usdt(bids, sell_qty)
        if proceeds is None or not order_size_ok(sell_rules, sell_qty, proceeds):
            continue
        after_trade = proceeds * (1 - sell_rate)
        # Return the USDT to the buying exchange so a repeatable cycle is priced.
        usdt_routes = chain_options(networks(sell, 'USDT'), networks(buy, 'USDT'), sell, after_trade)
        for usdt_net, final_usdt, usdt_cost in usdt_routes:
            price_buffer = TRADE * PRICE_BUFFER_PCT / 100
            profit = final_usdt - TRADE - price_buffer
            net = profit / TRADE * 100
            if best is None or net > best['net']:
                gross = (top_bid / top_ask - 1) * 100
                buy_vwap = TRADE / base
                sell_vwap = proceeds / sell_qty
                buy_slippage = max(Decimal(0), TRADE - base * top_ask)
                sell_slippage = max(Decimal(0), sell_qty * top_bid - proceeds)
                best = dict(symbol=symbol, buy=buy, sell=sell, net=net, profit=profit,
                            gross=gross, coin_net=coin_net, usdt_net=usdt_net,
                            coin_cost=coin_cost, usdt_cost=usdt_cost,
                            unsold_dust=transferred_qty - sell_qty,
                            buy_fee=buy_rate * 100, sell_fee=sell_rate * 100,
                            buy_price=buy_vwap, sell_price=sell_vwap,
                            sell_qty=sell_qty,
                            buy_slippage=buy_slippage, sell_slippage=sell_slippage,
                            price_buffer=price_buffer)
    return best


SPOT_SPOT_ENABLED = False  # Preserve legacy implementation; explicitly disabled.


def scan_once():
    if not SPOT_SPOT_ENABLED:
        return []
    if not all((BINANCE_KEY, BINANCE_SECRET, GATE_KEY, GATE_SECRET)):
        return []
    bd, gd = tickers()
    shortlist = []
    for symbol in bd.keys() & gd.keys():
        b, g = bd[symbol], gd[symbol]
        for buy, sell, ask, bid in (
            ('Binance', 'Gate', dec(b.get('askPrice')), dec(g.get('highest_bid'))),
            ('Gate', 'Binance', dec(g.get('lowest_ask')), dec(b.get('bidPrice'))),
        ):
            if ask and bid and ask > 0 and (bid / ask - 1) * 100 >= THRESH:
                shortlist.append(((bid / ask - 1), symbol, buy, sell, ask, bid))
    shortlist.sort(reverse=True)
    found = []
    for _, symbol, buy, sell, ask, bid in shortlist[:MAX_CANDIDATES]:
        try:
            result = estimate(symbol, buy, sell, ask, bid)
            if result and result['net'] >= THRESH:
                found.append(result)
        except (requests.RequestException, ValueError, KeyError, TypeError, RuntimeError):
            logging.warning('Skipped %s %s→%s: verified costs unavailable', symbol, buy, sell)
    found.sort(key=lambda x: x['net'], reverse=True)
    now = time.time()
    for item in found[:5]:
        key = (item['symbol'], item['buy'], item['sell'])
        if now - last_alert.get(key, 0) < COOLDOWN:
            continue
        text = (f"🔥 {item['symbol']} — купить {item['buy']} → продать {item['sell']}\n"
                f"Сделка: {TRADE:.0f} USDT; резерв: {RESERVE:.0f} USDT на каждой бирже.\n"
                f"Цена по глубине: покупка {item['buy_price']:.8g}, продажа {item['sell_price']:.8g} USDT.\n"
                f"Исходный спред лучших цен: {item['gross']:.2f}%.\n"
                f"Торговые комиссии (taker): {item['buy_fee']:.3f}% / {item['sell_fee']:.3f}%.\n"
                f"Монета: сеть {item['coin_net']}, вывод и зачисление {item['coin_cost']:.8g} {item['symbol'][:-4]}.\n"
                f"Остаток ниже шага ордера: {item['unsold_dust']:.8g} {item['symbol'][:-4]} "
                "(не включён в прибыль).\n"
                f"Возврат USDT: сеть {item['usdt_net']}, вывод и зачисление {item['usdt_cost']:.4f} USDT.\n"
                f"Проскальзывание по стаканам: {item['buy_slippage']:.4f} + "
                f"{item['sell_slippage']:.4f} USDT (уже включено в цены).\n"
                f"Защитный резерв на изменение цены: {PRICE_BUFFER_PCT:.2f}% "
                f"({item['price_buffer']:.2f} USDT).\n"
                f"Итог после всех расходов и резерва: {item['net']:.2f}% "
                f"(≈ {item['profit']:.2f} USDT).\n"
                "Последовательный перевод монеты и возврат USDT; цены и доступность сети могут измениться. "
                "Уведомление, без автоматических сделок.")
        telegram(text)
        last_alert[key] = now
    return found


def loop():
    first_success = True
    while True:
        try:
            found = scan_once()
            if first_success:
                logging.info('scan succeeded: %s verified opportunities', len(found))
                first_success = False
        except Exception:
            logging.exception('scan failed')
        time.sleep(SCAN)


scanner_api = None
scanner_api_lock = threading.Lock()


def current_scanner_api():
    global scanner_api
    with scanner_api_lock:
        if scanner_api is None:
            scanner_api = exchanges.ScannerAPI(__import__(__name__))
    return scanner_api


def basis_loop():
    if current_scanner_api().bingx.enabled:
        # One read-only probe shares the scanner's limiter; never records or alerts.
        import smoke_bingx
        report = smoke_bingx.probe(current_scanner_api().bingx)
        logging.info('BingX GET-only startup probe: %s',
                     {k: v.get('ok') for k, v in report.items() if isinstance(v, dict)})
    while True:
        try:
            results = basis.scan(current_scanner_api())
            # basis.scan logs candidates and actually dispatched conditional alerts.
        except Exception:
            logging.exception('basis scan failed')
        time.sleep(max(60, SCAN))


def paper_loop():
    import virtual
    recovered = False
    while True:
        try:
            if paper.storage_ready():
                if not recovered:
                    virtual.bootstrap()
                    recovered = True
                paper.poll(current_scanner_api())
                import virtual
                virtual.observe(current_scanner_api())
        except Exception:
            logging.exception('Virtual checkpoints unavailable; no historical price substituted')
        time.sleep(min(SCAN, 20))





@app.get('/healthz')
def healthz():
    return {'status': 'ok'}, 200


@app.get('/')
def home():
    return {'status': 'ok', 'scanner': 'Spot→Futures Binance-Gate-BingX', 'spot_spot_enabled': SPOT_SPOT_ENABLED, 'min_net_profit_pct': float(THRESH),
            'trade_usdt': float(TRADE), 'reserve_usdt_per_exchange': float(RESERVE),
            'price_buffer_pct': float(PRICE_BUFFER_PCT),
            'cost_data_ready': all((BINANCE_KEY, BINANCE_SECRET, GATE_KEY, GATE_SECRET)),
            'bingx_enabled': current_scanner_api().bingx.enabled,
            'bingx_credentials_ready': current_scanner_api().bingx.credentials_ready,
            'basis_mode': 'read-only', 'basis_leg_usdt': float(basis.LEG_USDT),
            'basis_reserve_usdt_per_exchange': float(basis.RESERVE_USDT),
            'paper_storage_ready': paper.storage_ready(),
            'virtual_deposit_usdt': float(__import__('virtual').deposit()),
            'virtual_capital_limit_usdt': float(__import__('virtual').deposit()/2)}


def telegram_updates():
    import virtual
    offset = 0
    ready_logged = False
    while True:
        try:
            offset = max(offset, virtual.cursor())
            if not ready_logged:
                logging.info('Virtual Telegram /open /close callbacks ready; persistent offset %s', offset)
                ready_logged = True
            response = requests.get(f'https://api.telegram.org/bot{TOKEN}/getUpdates',
                                    params={'timeout': 30, 'offset': offset}, timeout=35)
            if response.status_code != 200:
                logging.error('Telegram getUpdates HTTP %s', response.status_code)
                time.sleep(15)
                continue
            payload = response.json()
            if not payload.get('ok'):
                raise RuntimeError('Telegram getUpdates failed')
            for update in payload.get('result', []):
                offset = update['update_id'] + 1
                if not virtual.claim_update(update['update_id']):
                    continue
                if virtual.handle(current_scanner_api(), update, CHAT_ID):
                    callback = update.get('callback_query', {})
                    if callback.get('id'):
                        try:
                            requests.post(f'https://api.telegram.org/bot{TOKEN}/answerCallbackQuery', json={'callback_query_id':callback['id']}, timeout=10)
                        except Exception:
                            logging.warning('Virtual callback acknowledgment unavailable')
                    continue
                message = update.get('message', {})
                chat_id = message.get('chat', {}).get('id')
                command = message.get('text', '').split(maxsplit=1)
                if command and command[0].split('@')[0] == '/start' and chat_id:
                    telegram('✅ Spot → Futures Binance / Gate / BingX. POSITIVE / NEGATIVE. PAPER / VIRTUAL ONLY. Spot → Spot отключён.', chat_id)
                    logging.info('Telegram /start answered')
                if command and command[0].split('@')[0] == '/stats' and chat_id and str(chat_id) == str(CHAT_ID):
                    try:
                        symbol = command[1].strip().upper() if len(command) > 1 else None
                        if symbol and not symbol.endswith('USDT'):
                            symbol += 'USDT'
                        telegram(paper.stats_line(symbol=symbol), chat_id)
                    except Exception:
                        telegram('История пока недоступна: постоянное хранилище не подключено.', chat_id)
                offset = update['update_id'] + 1
        except Exception as exc:
            logging.error('telegram updates failed: %s', type(exc).__name__)
            time.sleep(5)


def log_webhook_owner():
    if not TOKEN:
        logging.error('TELEGRAM_BOT_TOKEN is missing')
        return False
    try:
        response = requests.get(f'https://api.telegram.org/bot{TOKEN}/getWebhookInfo', timeout=10)
        data = response.json() if response.status_code == 200 else {}
        if not data.get('ok'):
            logging.error('Telegram webhook check failed: HTTP %s', response.status_code)
            return False
        url = data.get('result', {}).get('url', '')
        logging.info('Telegram webhook host: %s', urlparse(url).hostname or 'none')
        return not bool(url)
    except Exception as exc:
        logging.error('Telegram webhook check failed: %s', type(exc).__name__)
        return False


if __name__ == '__main__':
    if not paper.storage_ready():
        logging.warning('Virtual episode storage unavailable: Spot/Futures alerts paused')
    if SPOT_SPOT_ENABLED:
        threading.Thread(target=loop, daemon=True).start()
    import risk_monitor
    risk_monitor.Monitor(__import__(__name__), shared_bingx=current_scanner_api().bingx).start()
    threading.Thread(target=basis_loop, daemon=True).start()
    threading.Thread(target=paper_loop, daemon=True).start()
    if log_webhook_owner():
        threading.Thread(target=telegram_updates, daemon=True).start()
    else:
        logging.warning('Telegram polling disabled: existing webhook or token problem')
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '10000')))
