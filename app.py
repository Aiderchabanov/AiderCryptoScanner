import hashlib
import hmac
import base64
import logging
import os
import threading
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode, urlparse

import requests
from flask import Flask

BINANCE = 'https://api.binance.com'
KUCOIN = 'https://api.kucoin.com'
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
BINANCE_KEY = os.getenv('BINANCE_API_KEY', '')
BINANCE_SECRET = os.getenv('BINANCE_API_SECRET', '')
KUCOIN_KEY = os.getenv('KUCOIN_API_KEY', '')
KUCOIN_SECRET = os.getenv('KUCOIN_API_SECRET', '')
KUCOIN_PASSPHRASE = os.getenv('KUCOIN_API_PASSPHRASE', '')
KUCOIN_KEY_VERSION = os.getenv('KUCOIN_API_KEY_VERSION', '2')
THRESH = Decimal(os.getenv('MIN_NET_PROFIT_PCT', '0.5'))
TRADE = Decimal(os.getenv('TRADE_USDT', '1000'))
COOLDOWN = int(os.getenv('ALERT_COOLDOWN_SEC', '1800'))
SCAN = int(os.getenv('SCAN_INTERVAL_SEC', '20'))
MAX_CANDIDATES = int(os.getenv('MAX_CANDIDATES_PER_SCAN', '10'))
last_alert = {}
cache = {}
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


def get_json(url, **kwargs):
    response = requests.get(url, timeout=10, **kwargs)
    response.raise_for_status()
    return response.json()


def cached(key, ttl, loader):
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


def kucoin(path, params=None, signed=False):
    query = urlencode(params or {})
    headers = {}
    if signed:
        if not all((KUCOIN_KEY, KUCOIN_SECRET, KUCOIN_PASSPHRASE)):
            raise RuntimeError('KuCoin read-only credentials missing')
        timestamp = str(int(time.time() * 1000))
        def sign(value):
            digest = hmac.new(KUCOIN_SECRET.encode(), value.encode(), hashlib.sha256).digest()
            return base64.b64encode(digest).decode()
        headers = {'KC-API-KEY': KUCOIN_KEY,
                   'KC-API-SIGN': sign(timestamp + 'GET' + path + ('?' + query if query else '')),
                   'KC-API-TIMESTAMP': timestamp,
                   'KC-API-PASSPHRASE': sign(KUCOIN_PASSPHRASE),
                   'KC-API-KEY-VERSION': KUCOIN_KEY_VERSION}
    payload = get_json(KUCOIN + path, params=params, headers=headers)
    if not isinstance(payload, dict) or payload.get('code') != '200000':
        raise RuntimeError('KuCoin API rejected request')
    return payload['data']


def tickers():
    b = binance('/api/v3/ticker/bookTicker')
    k = kucoin('/api/v1/market/allTickers')['ticker']
    if not isinstance(b, list) or not isinstance(k, list):
        raise RuntimeError('Invalid market ticker data')
    return ({x['symbol']: x for x in b if x.get('symbol', '').endswith('USDT')},
            {x['symbol'].replace('-', ''): x for x in k
             if x.get('symbol', '').endswith('-USDT')})


def telegram(msg, chat_id=None):
    if not TOKEN or not (chat_id or CHAT_ID):
        return
    response = requests.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage',
                             json={'chat_id': chat_id or CHAT_ID, 'text': msg}, timeout=10)
    if response.status_code != 200 or not response.json().get('ok'):
        raise RuntimeError(f'Telegram sendMessage HTTP {response.status_code}')


def fee(exchange, symbol):
    def load():
        if exchange == 'Binance':
            rows = binance('/sapi/v1/asset/tradeFee', {'symbol': symbol}, True)
            row = next((x for x in rows if x.get('symbol') == symbol), None)
            value = row and positive(row.get('takerCommission'))
        else:
            pair = symbol[:-4] + '-USDT'
            rows = kucoin('/api/v1/trade-fees', {'symbols': pair}, True)
            row = next((x for x in rows if x.get('symbol') == pair), None)
            value = row and positive(row.get('takerFeeRate'))
        if value is None or value >= 1:
            raise RuntimeError('Trading fee unavailable')
        return value
    return cached(('fee', exchange, symbol), 3600, load)


def networks(exchange, coin):
    if exchange == 'KuCoin':
        return cached(('chains', exchange, coin), 300,
                      lambda: kucoin('/api/v3/currencies/' + coin).get('chains', []))
    def load_all():
        rows = binance('/sapi/v1/capital/config/getall', signed=True)
        if not isinstance(rows, list):
            raise RuntimeError('Binance chain data unavailable')
        return {x['coin']: x.get('networkList', []) for x in rows if 'coin' in x}
    return cached(('chains', 'Binance'), 300, load_all).get(coin, [])


def chain_id(chain):
    raw = str(chain or '').upper().replace(' ', '').replace('-', '')
    aliases = {'ERC20': 'ETH', 'ETHEREUM': 'ETH', 'TRC20': 'TRX', 'TRON': 'TRX',
               'BEP20(BSC)': 'BSC', 'BEP20': 'BSC', 'BNBSMARTCHAIN': 'BSC',
               'SOLANA': 'SOL', 'MATIC': 'POLYGON', 'POLYGONPOS': 'POLYGON',
               'ARBITRUMONE': 'ARBITRUM', 'OP': 'OPTIMISM'}
    return aliases.get(raw, raw)


def chain_options(src, dst, source_exchange, amount):
    dest_exchange = 'KuCoin' if source_exchange == 'Binance' else 'Binance'
    def field(row, exchange, kind):
        if exchange == 'Binance':
            return row.get({'id': 'network', 'withdraw': 'withdrawEnable',
                            'deposit': 'depositEnable', 'fee': 'withdrawFee',
                            'min': 'withdrawMin', 'max': 'withdrawMax',
                            'deposit_min': 'depositMin'}[kind])
        return row.get({'id': 'chainId', 'withdraw': 'isWithdrawEnabled',
                        'deposit': 'isDepositEnabled', 'fee': 'withdrawalMinFee',
                        'min': 'withdrawalMinSize', 'max': 'maxWithdraw',
                        'deposit_min': 'depositMinSize'}[kind])
    found = []
    for a in src:
        name_a = chain_id(field(a, source_exchange, 'id'))
        if not name_a:
            continue
        if field(a, source_exchange, 'withdraw') is not True:
            continue
        fixed = positive(field(a, source_exchange, 'fee'))
        pct = positive(a.get('withdrawFeeRate')) if source_exchange == 'KuCoin' else Decimal(0)
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
            received = amount - fixed - amount * pct
            deposit_min = positive(field(b, dest_exchange, 'deposit_min') or '0')
            if received > 0 and deposit_min is not None and received >= deposit_min:
                found.append((name_a, received, amount - received))
    return found


def orderbook(exchange, symbol):
    if exchange == 'KuCoin':
        pair = symbol[:-4] + '-USDT'
        result = kucoin('/api/v3/market/orderbook/level2', {'symbol': pair}, True)
        return result.get('asks', []), result.get('bids', [])
    result = binance('/api/v3/depth', {'symbol': symbol, 'limit': 500})
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
    buy_rate, sell_rate = fee(buy, symbol), fee(sell, symbol)
    coin_to_transfer = base * (1 - buy_rate)
    coin_routes = chain_options(networks(buy, coin), networks(sell, coin), buy, coin_to_transfer)
    best = None
    for coin_net, sell_qty, coin_cost in coin_routes:
        proceeds = sell_for_usdt(bids, sell_qty)
        if proceeds is None:
            continue
        after_trade = proceeds * (1 - sell_rate)
        # Return the USDT to the buying exchange so a repeatable cycle is priced.
        usdt_routes = chain_options(networks(sell, 'USDT'), networks(buy, 'USDT'), sell, after_trade)
        for usdt_net, final_usdt, usdt_cost in usdt_routes:
            profit = final_usdt - TRADE
            net = profit / TRADE * 100
            if best is None or net > best['net']:
                gross = (top_bid / top_ask - 1) * 100
                best = dict(symbol=symbol, buy=buy, sell=sell, net=net, profit=profit,
                            gross=gross, coin_net=coin_net, usdt_net=usdt_net,
                            coin_cost=coin_cost, usdt_cost=usdt_cost,
                            buy_fee=buy_rate * 100, sell_fee=sell_rate * 100)
    return best


def scan_once():
    if not all((BINANCE_KEY, BINANCE_SECRET, KUCOIN_KEY, KUCOIN_SECRET, KUCOIN_PASSPHRASE)):
        return []
    bd, kd = tickers()
    shortlist = []
    for symbol in bd.keys() & kd.keys():
        b, k = bd[symbol], kd[symbol]
        for buy, sell, ask, bid in (
            ('Binance', 'KuCoin', dec(b.get('askPrice')), dec(k.get('buy'))),
            ('KuCoin', 'Binance', dec(k.get('sell')), dec(b.get('bidPrice'))),
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
        text = (f"🔥 {item['symbol']} — расчётная чистая прибыль {item['net']:.2f}%"
                f" (≈ ${item['profit']:.2f} на ${TRADE:.0f})\n"
                f"Купить: {item['buy']} → продать: {item['sell']}\n"
                f"Монета: сеть {item['coin_net']}, вывод {item['coin_cost']:.8g} {item['symbol'][:-4]}\n"
                f"Возврат USDT: сеть {item['usdt_net']}, вывод {item['usdt_cost']:.4f} USDT\n"
                f"Торговые комиссии: {item['buy_fee']:.3f}% / {item['sell_fee']:.3f}%\n"
                f"Спред лучших цен: {item['gross']:.2f}%; исполнение рассчитано по глубине стаканов.\n"
                "Оценка на текущем стакане, цена и доступность вывода могут измениться до сделки.")
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


@app.get('/')
def home():
    return {'status': 'ok', 'scanner': 'Binance-KuCoin', 'min_net_profit_pct': float(THRESH),
            'cost_data_ready': all((BINANCE_KEY, BINANCE_SECRET, KUCOIN_KEY, KUCOIN_SECRET, KUCOIN_PASSPHRASE))}


def telegram_updates():
    offset = 0
    while True:
        try:
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
                message = update.get('message', {})
                chat_id = message.get('chat', {}).get('id')
                command = message.get('text', '').split(maxsplit=1)
                if command and command[0].split('@')[0] == '/start' and chat_id:
                    telegram(f'✅ Aider Crypto Scanner запущен. Проверка чистой прибыли ≥ {THRESH}% по Binance ↔ KuCoin.', chat_id)
                    logging.info('Telegram /start answered')
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
    threading.Thread(target=loop, daemon=True).start()
    if log_webhook_owner():
        threading.Thread(target=telegram_updates, daemon=True).start()
    else:
        logging.warning('Telegram polling disabled: existing webhook or token problem')
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '10000')))
