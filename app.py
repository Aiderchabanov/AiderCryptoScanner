import os, time, logging, requests
from flask import Flask

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

def fetch():
    b=requests.get(BYBIT,timeout=10).json()['result']['list']
    m=requests.get(MEXC,timeout=10).json()
    bd={x['symbol']:x for x in b if x['symbol'].endswith('USDT')}
    md={x['symbol']:x for x in m if x['symbol'].endswith('USDT')}
    return bd,md

def telegram(msg):
    if not TOKEN or not CHAT_ID: return
    requests.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage',json={'chat_id':CHAT_ID,'text':msg},timeout=10).raise_for_status()

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

if __name__=='__main__':
    import threading
    threading.Thread(target=loop,daemon=True).start()
    app.run(host='0.0.0.0',port=int(os.getenv('PORT','10000')))
