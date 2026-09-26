import os, time, logging, requests
from flask import Flask
from urllib.parse import urlparse

BYBIT='https://api.bybit.com/v5/market/tickers?category=spot'
MEXC='https://api.mexc.com/api/v3/ticker/bookTicker'
TOKEN=os.getenv('TELEGRAM_BOT_TOKEN','')
CHAT_ID=os.getenv('TELEGRAM_CHAT_ID','')
THRESH=float(os.getenv('MIN_SPREAD_PCT','2.0'))
TRADE=float(os.getenv('TRADE_USDT','1000'))
BYBIT_FEE=float(os.getenv('BYBIT_FEE_PCT','0.10'))
MEXC_FEE=float(os.getenv('MEXC_FEE_PCT','0.10'))
COOLDOWN=int(os.getenv('ALERT_COOLDOWN_SEC','1800'))
MIN_TOP=float(os.getenv('MIN_TOP_BOOK_USDT','1000'))
SCAN=int(os.getenv('SCAN_INTERVAL_SEC','20'))
last_alert={}
app=Flask(__name__)
logging.basicConfig(level=logging.INFO)

def f(x):
    try: return float(x)
    except: return 0.0

def market_json(url):
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(f'Market API returned invalid JSON ({response.status_code}, {response.headers.get("Content-Type", "unknown")})') from exc

def fetch():
    last_error = None
    for url in (BYBIT, BYBIT.replace('api.bybit.com', 'api.bytick.com')):
        try:
            payload = market_json(url)
            if payload.get('retCode') != 0 or not isinstance(payload.get('result', {}).get('list'), list):
                raise RuntimeError(f'Bybit API error: {payload.get("retMsg", "invalid ticker data")}')
            b = payload['result']['list']
            break
        except (requests.RequestException, ValueError, RuntimeError, AttributeError) as exc:
            last_error = exc
    else:
        raise RuntimeError('Bybit market data unavailable') from last_error
    m = market_json(MEXC)
    if not isinstance(m, list):
        raise RuntimeError('MEXC returned invalid ticker data')
    bd={x['symbol']:x for x in b if x['symbol'].endswith('USDT')}
    md={x['symbol']:x for x in m if x['symbol'].endswith('USDT')}
    return bd,md

def telegram(msg):
    if not TOKEN or not CHAT_ID: return
    response = requests.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage',json={'chat_id':CHAT_ID,'text':msg},timeout=10)
    if response.status_code != 200 or not response.json().get('ok'):
        raise RuntimeError(f'Telegram sendMessage HTTP {response.status_code}')

def candidate(symbol,buy_exchange,buy_ask,buy_qty,sell_exchange,sell_bid,sell_qty,buy_fee,sell_fee):
    if buy_ask<=0 or sell_bid<=0: return None
    gross=(sell_bid/buy_ask-1)*100
    net=gross-buy_fee-sell_fee
    buy_depth=buy_ask*buy_qty; sell_depth=sell_bid*sell_qty
    if gross<THRESH or min(buy_depth,sell_depth)<MIN_TOP: return None
    est=TRADE*net/100
    return (net,gross,est,buy_depth,sell_depth,f'🔥 {symbol}\nКупить: {buy_exchange} @ {buy_ask:g}\nПродать: {sell_exchange} @ {sell_bid:g}\nСпред: {gross:.2f}%\nПосле торговых комиссий*: {net:.2f}% ≈ ${est:.2f} на ${TRADE:.0f}\nTop-book: buy ${buy_depth:,.0f} / sell ${sell_depth:,.0f}\n\n⚠️ *Комиссия вывода/сеть НЕ учтены. Перед сделкой обязательно проверить ввод/вывод одной сети и глубину стакана.')

def scan_once():
    bd,md=fetch(); now=time.time(); found=[]
    for s in bd.keys() & md.keys():
        b,m=bd[s],md[s]
        vals=[
          candidate(s,'Bybit',f(b.get('ask1Price')),f(b.get('ask1Size')),'MEXC',f(m.get('bidPrice')),f(m.get('bidQty')),BYBIT_FEE,MEXC_FEE),
          candidate(s,'MEXC',f(m.get('askPrice')),f(m.get('askQty')),'Bybit',f(b.get('bid1Price')),f(b.get('bid1Size')),MEXC_FEE,BYBIT_FEE)
        ]
        for c in vals:
            if c: found.append((s,c))
    found.sort(key=lambda x:x[1][0],reverse=True)
    for s,c in found[:5]:
        key=(s,c[5].split('\n')[1],round(c[1],1))
        if now-last_alert.get(key,0)>=COOLDOWN:
            telegram(c[5]); last_alert[key]=now
    return found

def loop():
    while True:
        try: scan_once()
        except Exception: logging.exception('scan failed')
        time.sleep(SCAN)

@app.get('/')
def home(): return {'status':'ok','scanner':'Bybit-MEXC','threshold_pct':THRESH}
def telegram_updates():
    offset = 0
    while True:
        try:
            response = requests.get(
                f"https://api.telegram.org/bot{TOKEN}/getUpdates",
                params={"timeout": 30, "offset": offset},
                timeout=35
            )
            if response.status_code != 200:
                try:
                    detail = response.json().get('description', 'unknown error')
                except ValueError:
                    detail = 'non-JSON response'
                logging.error('Telegram getUpdates HTTP %s: %s', response.status_code, detail)
                time.sleep(15)
                continue
            r = response.json()
            if not r.get("ok"):
                raise RuntimeError(f'Telegram getUpdates failed: {r.get("description", "unknown error")}')

            for update in r.get("result", []):
                message = update.get("message", {})
                text = message.get("text", "")
                chat_id = message.get("chat", {}).get("id")

                if text and text.split(maxsplit=1)[0].split('@')[0] == "/start" and chat_id:
                    reply = requests.post(
                        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                        json={
                            "chat_id": chat_id,
                            "text": "✅ Aider Crypto Scanner запущен.\nСканер Bybit ↔ MEXC проверяет рынок."
                        },
                        timeout=10
                    )
                    if reply.status_code != 200 or not reply.json().get("ok"):
                        raise RuntimeError('Telegram sendMessage failed')
                    logging.info('Telegram /start answered')
                offset = update["update_id"] + 1
        except Exception as exc:
            logging.error("telegram updates failed: %s", type(exc).__name__)
            time.sleep(5)
def log_webhook_owner():
    if not TOKEN:
        logging.error('TELEGRAM_BOT_TOKEN is missing')
        return
    try:
        response = requests.get(f'https://api.telegram.org/bot{TOKEN}/getWebhookInfo', timeout=10)
        data = response.json() if response.status_code == 200 else {}
        url = data.get('result', {}).get('url', '')
        logging.info('Telegram webhook host: %s', urlparse(url).hostname or 'none')
    except Exception as exc:
        logging.error('Telegram webhook check failed: %s', type(exc).__name__)

if __name__ == '__main__':
    log_webhook_owner()
    import threading
    threading.Thread(target=loop, daemon=True).start()
    threading.Thread(target=telegram_updates, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '10000')))
