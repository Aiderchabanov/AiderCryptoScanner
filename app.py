import hashlib
import hmac
import logging
import os
import threading
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode, urlparse

import requests
from flask import Flask

BYBIT = 'https://api.bybit.com'
MEXC = 'https://api.mexc.com'
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
BYBIT_KEY = os.getenv('BYBIT_API_KEY', '')
BYBIT_SECRET = os.getenv('BYBIT_API_SECRET', '')
MEXC_KEY = os.getenv('MEXC_API_KEY', '')
MEXC_SECRET = os.getenv('MEXC_API_SECRET', '')
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


def bybit(path, params=None, signed=False):
    query = urlencode(params or {})
    headers = {}
    if signed:
        if not BYBIT_KEY or not BYBIT_SECRET:
            raise RuntimeError('Bybit read-only API credentials missing')
        timestamp = str(int(time.time() * 1000))
        window = '5000'
        signature = hmac.new(BYBIT_SECRET.encode(), (timestamp + BYBIT_KEY + window + query).encode(), hashlib.sha256).hexdigest()
        headers = {'X-BAPI-API-KEY': BYBIT_KEY, 'X-BAPI-TIMESTAMP': timestamp,
                   'X-BAPI-RECV-WINDOW': window, 'X-BAPI-SIGN': signature}
    # Never log headers, query signatures, or raw server responses.
    payload = get_json(BYBIT + path, params=params, headers=headers)
    if payload.get('retCode') != 0:
        raise RuntimeError('Bybit API rejected request')
    return payload['result']


def mexc(path, params=None, signed=False):
    params = dict(params or {})
    headers = {}
    if signed:
        if not MEXC_KEY or not MEXC_SECRET:
            raise RuntimeError('MEXC read-only API credentials missing')
        params['timestamp'] = int(time.time() * 1000)
        query = urlencode(params)
        params['signature'] = hmac.new(MEXC_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
        headers['X-MEXC-APIKEY'] = MEXC_KEY
    payload = get_json(MEXC + path, params=params, headers=headers)
    if signed and isinstance(payload, dict) and payload.get('code', 0) not in (0, 200):
        raise RuntimeError('MEXC API rejected request')
    return payload


def tickers():
    try:
        b = bybit('/v5/market/tickers', {'category': 'spot'})['list']
    except (requests.RequestException, RuntimeError):
        # Preserve the public market-data fallback used by the existing service.
        payload = get_json('https://api.bytick.com/v5/market/tickers', params={'category': 'spot'})
        if payload.get('retCode') != 0:
            raise RuntimeError('Bybit market data unavailable')
        b = payload['result']['list']
    m = mexc('/api/v3/ticker/bookTicker')
    if not isinstance(b, list) or not isinstance(m, list):
        raise RuntimeError('Invalid market ticker data')
    return ({x['symbol']: x for x in b if x.get('symbol', '').endswith('USDT')},
            {x['symbol']: x for x in m if x.get('symbol', '').endswith('USDT')})


def telegram(msg, chat_id=None):
    if not TOKEN or not (chat_id or CHAT_ID):
        return
    response = requests.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage',
                             json={'chat_id': chat_id or CHAT_ID, 'text': msg}, timeout=10)
    if response.status_code != 200 or not response.json().get('ok'):
        raise RuntimeError(f'Telegram sendMessage HTTP {response.status_code}')


def fee(exchange, symbol):
    def load():
        if exchange == 'Bybit':
            rows = bybit('/v5/account/fee-rate', {'category': 'spot', 'symbol': symbol}, True)['list']
            row = next((x for x in rows if x.get('symbol') == symbol), None)
            value = row and positive(row.get('takerFeeRate'))
        else:
            data = mexc('/api/v3/tradeFee', {'symbol': symbol}, True)
            row = data.get('data') if isinstance(data, dict) else None
            value = row and positive(row.get('takerCommission'))
        if value is None or value >= 1:
            raise RuntimeError('Trading fee unavailable')
        return value
    return cached(('fee', exchange, symbol), 3600, load)


def networks(exchange, coin):
    if exchange == 'Bybit':
        def load():
            rows = bybit('/v5/asset/coin/query-info', {'coin': coin}, True)['rows']
            row = next((x for x in rows if x.get('coin') == coin), None)
            return row.get('chains', []) if row else []
        return cached(('chain', exchange, coin), 300, load)

    def load_all():
        rows = mexc('/api/v3/capital/config/getall', signed=True)
        if not isinstance(rows, list):
            raise RuntimeError('MEXC chain data unavailable')
        return {x['coin']: x.get('networkList', []) for x in rows if 'coin' in x}
    return cached(('chains', 'MEXC'), 300, load_all).get(coin, [])


def chain_id(chain):
    raw = str(chain or '').upper().replace(' ', '')
    aliases = {'ERC20': 'ETH', 'ETHEREUM': 'ETH', 'TRC20': 'TRX', 'TRON': 'TRX',
               'BEP20(BSC)': 'BSC', 'BEP20': 'BSC', 'SOLANA': 'SOL',
               'MATIC': 'POLYGON'}
    return aliases.get(raw, raw)


def chain_options(src, dst, source_exchange, amount):
    found = []
    for a in src:
        name_a = chain_id(a.get('chain') if source_exchange == 'Bybit' else a.get('netWork') or a.get('network'))
        if not name_a:
            continue
        active_a = a.get('chainWithdraw') == '1' if source_exchange == 'Bybit' else a.get('withdrawEnable') is True
        if not active_a:
            continue
        fixed = positive(a.get('withdrawFee'))
        pct = positive(a.get('withdrawPercentageFee', '0')) if source_exchange == 'Bybit' else Decimal(0)
        minimum = positive(a.get('withdrawMin'))
        maximum = positive(a.get('withdrawMax'))
        if fixed is None or pct is None or pct >= 1 or minimum is None or amount < minimum or (maximum and amount > maximum):
            continue
        for b in dst:
            name_b = chain_id(b.get('chain') if source_exchange != 'Bybit' else b.get('netWork') or b.get('network'))
            active_b = b.get('chainDeposit') == '1' if source_exchange != 'Bybit' else b.get('depositEnable') is True
            if not active_b or name_a != name_b:
                continue
            ca, cb = str(a.get('contractAddress') or a.get('contract') or '').lower(), str(b.get('contractAddress') or b.get('contract') or '').lower()
            if ca and cb and ca != cb:
                continue
            received = (amount - fixed) * (1 - pct)
            deposit_min = positive(b.get('depositMin', '0'))
            if received > 0 and deposit_min is not None and received >= deposit_min:
                found.append((name_a, received, amount - received))
    return found


def orderbook(exchange, symbol):
    if exchange == 'Bybit':
        result = bybit('/v5/market/orderbook', {'category': 'spot', 'symbol': symbol, 'limit': 200})
        return result.get('a', []), result.get('b', [])
    result = mexc('/api/v3/depth', {'symbol': symbol, 'limit': 200})
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
    if not asks or not bids or dec(asks[0][0]) != top_ask or dec(bids[0][0]) != top_bid:
        return None  # tickers and orderbooks are already inconsistent
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
    if not all((BYBIT_KEY, BYBIT_SECRET, MEXC_KEY, MEXC_SECRET)):
        return []
    bd, md = tickers()
    shortlist = []
    for symbol in bd.keys() & md.keys():
        b, m = bd[symbol], md[symbol]
        for buy, sell, ask, bid in (
            ('Bybit', 'MEXC', dec(b.get('ask1Price')), dec(m.get('bidPrice'))),
            ('MEXC', 'Bybit', dec(m.get('askPrice')), dec(b.get('bid1Price'))),
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
    return {'status': 'ok', 'scanner': 'Bybit-MEXC', 'min_net_profit_pct': float(THRESH),
            'cost_data_ready': all((BYBIT_KEY, BYBIT_SECRET, MEXC_KEY, MEXC_SECRET))}


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
                if message.get('text', '').split(maxsplit=1)[0].split('@')[0] == '/start' and chat_id:
                    telegram('✅ Aider Crypto Scanner запущен. Проверка чистой прибыли ≥ 0,5% по Bybit ↔ MEXC.', chat_id)
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
