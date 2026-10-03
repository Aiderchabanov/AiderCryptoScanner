"""Independent public GET API health checks. Never invokes a trading scanner."""
import json
import logging
import math
import os
import re
import threading
import time
from decimal import Decimal
from email.utils import parsedate_to_datetime
import requests
import paper
import bingx

EXCHANGES=('Binance','Gate','BingX')
ENDPOINTS={
 'Binance':[
  ('Spot market','https://api.binance.com/api/v3/ticker/bookTicker',{'symbol':'BTCUSDT'},'ticker'),
  ('Spot depth','https://api.binance.com/api/v3/depth',{'symbol':'BTCUSDT','limit':5},'depth'),
  ('Futures market','https://fapi.binance.com/fapi/v1/ticker/bookTicker',{'symbol':'BTCUSDT'},'ticker'),
  ('Futures depth','https://fapi.binance.com/fapi/v1/depth',{'symbol':'BTCUSDT','limit':5},'depth'),
  ('Funding','https://fapi.binance.com/fapi/v1/premiumIndex',{'symbol':'BTCUSDT'},'funding')],
 'Gate':[
  ('Spot market','https://api.gateio.ws/api/v4/spot/tickers',{'currency_pair':'BTC_USDT'},'ticker'),
  ('Spot depth','https://api.gateio.ws/api/v4/spot/order_book',{'currency_pair':'BTC_USDT','limit':5,'with_id':'true'},'depth'),
  ('Futures market','https://api.gateio.ws/api/v4/futures/usdt/tickers',{'contract':'BTC_USDT'},'ticker'),
  ('Futures depth','https://api.gateio.ws/api/v4/futures/usdt/order_book',{'contract':'BTC_USDT','limit':5},'depth'),
  ('Funding','https://api.gateio.ws/api/v4/futures/usdt/contracts/BTC_USDT',{},'funding')],
 'BingX':[
  ('Spot market',bingx.BASE_URL+bingx.PATHS['spot_book'],{'symbol':'BTC-USDT'},'ticker'),
  ('Spot depth',bingx.BASE_URL+bingx.PATHS['spot_depth'],{'symbol':'BTC-USDT','limit':5},'depth'),
  ('Futures market',bingx.BASE_URL+bingx.PATHS['futures_book'],{'symbol':'BTC-USDT'},'ticker'),
  ('Futures depth',bingx.BASE_URL+bingx.PATHS['futures_depth'],{'symbol':'BTC-USDT','limit':5},'depth'),
  ('Funding',bingx.BASE_URL+bingx.PATHS['funding'],{'symbol':'BTC-USDT'},'funding')]
}
SCHEMA='''
CREATE TABLE IF NOT EXISTS risk_checks (
 exchange TEXT NOT NULL, component TEXT NOT NULL, state TEXT NOT NULL,
 failures INTEGER NOT NULL DEFAULT 0, last_ok DOUBLE PRECISION,
 first_failure DOUBLE PRECISION, last_check DOUBLE PRECISION NOT NULL,
 status TEXT NOT NULL, alerted INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(exchange,component)
);
CREATE TABLE IF NOT EXISTS risk_hosts (
 host TEXT PRIMARY KEY, until_at DOUBLE PRECISION NOT NULL, status TEXT NOT NULL
);
'''

class HealthError(RuntimeError):
    def __init__(self,status,retry=0):
        self.status=status;self.retry=retry
        super().__init__(status) # Sanitized: no URLs, payloads or credentials.


def ensure(db):
    if getattr(db,'is_postgres',False):
        db.execute("SELECT pg_advisory_xact_lock(hashtext('risk-monitor-schema'))")
    for stmt in SCHEMA.split(';'):
        if stmt.strip(): db.execute(stmt)


def retry_seconds(value,now=None):
    now=time.time() if now is None else now
    try: result=float(value)
    except (ValueError,TypeError):
        try: result=parsedate_to_datetime(value).timestamp()-now
        except (ValueError,TypeError,OverflowError): result=0
    return max(0,result) if math.isfinite(result) else 0


def value(raw,positive=False):
    try: n=Decimal(str(raw))
    except Exception: raise HealthError('UNKNOWN_DATA') from None
    if not n.is_finite() or (positive and n<=0): raise HealthError('UNKNOWN_DATA')
    return n


def validate(exchange,kind,raw):
    if exchange=='BingX':
        if not isinstance(raw,dict): raise HealthError('INVALID_DATA')
        code=raw.get('code')
        if code!=0:
            if code==100410: raise HealthError('RATE_LIMIT API 100410',60)
            if code in (100001,100004,100412,100413,100419): raise HealthError('AUTH API '+str(code),3600)
            raise HealthError('API_REJECTED '+str(code))
        raw=raw.get('data')
        if kind=='ticker' and isinstance(raw,dict) and 'book_ticker' in raw: raw=raw['book_ticker']
    if isinstance(raw,list):
        if not raw: raise HealthError('MARKET_DATA_UNAVAILABLE')
        raw=raw[0]
    if not isinstance(raw,dict): raise HealthError('MARKET_DATA_UNAVAILABLE')
    # Only actual quote/snapshot timestamps, never the last funding settlement.
    keys=('E','T','ts','current','time') if kind!='funding' else ('time',)
    timestamps=[raw[k] for k in keys if raw.get(k) is not None]
    if exchange=='BingX' and kind=='depth' and not timestamps: raise HealthError('FRESHNESS_UNKNOWN')
    for timestamp in timestamps:
        t=float(value(timestamp,True));t=t/1000 if t>100000000000 else t
        if not -5<=time.time()-t<=30: raise HealthError('STALE_MARKET_DATA')
    if kind=='ticker':
        ask=raw.get('askPrice',raw.get('lowest_ask',raw.get('ask_price')))
        bid=raw.get('bidPrice',raw.get('highest_bid',raw.get('bid_price')))
        if value(bid,True)>value(ask,True): raise HealthError('CROSSED_MARKET_DATA')
    elif kind=='depth':
        sides=[]
        for key in ('asks','bids'):
            levels=raw.get(key)
            if not isinstance(levels,list) or not levels: raise HealthError('DEPTH_UNAVAILABLE')
            prices=[]
            for row in levels:
                p,q=(row.get('p'),row.get('s')) if isinstance(row,dict) else row
                prices.append(value(p,True));value(abs(value(q)),True)
            sides.append(prices)
        if max(sides[1])>min(sides[0]): raise HealthError('CROSSED_MARKET_DATA')
    else:
        rate=raw.get('lastFundingRate') if exchange!='Gate' else raw.get('funding_rate_next')
        if exchange=='Gate' and rate in (None,''): rate=raw.get('funding_rate')
        if abs(value(rate))>Decimal('.1'): raise HealthError('INVALID_FUNDING')
        future=raw.get('funding_next_apply') if exchange=='Gate' else raw.get('nextFundingTime')
        t=float(value(future,True));t=t/1000 if exchange!='Gate' else t
        if t<=time.time(): raise HealthError('FUNDING_UNAVAILABLE')
    return True


class Monitor:
    def __init__(self,api,path=None,shared_bingx=None):
        self.api=api;self.path=path;self.bingx=shared_bingx
        self.interval=max(60,int(os.getenv('RISK_MONITOR_INTERVAL_SEC','120')))
        self.locks={name:threading.Lock() for name in EXCHANGES}
        self.next_request={name:0 for name in EXCHANGES}

    def blocked(self,host):
        with paper.session(self.path) as db:
            ensure(db)
            row=db.execute('SELECT * FROM risk_hosts WHERE host=?',(host,)).fetchone()
        if row and row['until_at']>time.time(): raise HealthError('COOLDOWN '+row['status'])

    def block(self,host,delay,status):
        until=time.time()+delay
        with paper.session(self.path) as db:
            ensure(db)
            db.execute('''INSERT INTO risk_hosts (host,until_at,status) VALUES (?,?,?)
                          ON CONFLICT(host) DO UPDATE SET until_at=excluded.until_at,status=excluded.status
                          WHERE risk_hosts.until_at<excluded.until_at''',(host,until,status))

    def request(self,exchange,url,params):
        allowed={entry[1] for entry in ENDPOINTS[exchange]}
        if url not in allowed: raise HealthError('ENDPOINT_NOT_ALLOWED')
        host=url.split('/')[2]
        self.blocked(host)
        client=self.bingx if exchange=='BingX' else None
        limiter=client._lock if client else self.locks[exchange]
        with limiter:
            if client and time.monotonic()<client._blocked_until: raise HealthError('COOLDOWN SHARED_BINGX_API')
            deadline=client._next_request if client else self.next_request[exchange]
            wait=max(0,deadline-time.monotonic())
            if wait: time.sleep(wait)
            pace=max(1.05,client.interval) if client else 1.05
            if client: client._next_request=time.monotonic()+pace
            else: self.next_request[exchange]=time.monotonic()+pace
            started=time.monotonic()
            try:
                if exchange=='Binance':
                    # Reuse existing public GET helper solely to respect its host bans.
                    data=self.api.get_json(url,params=params)
                else:
                    public_params=dict(params)
                    if exchange=='BingX': public_params.update(timestamp=int(time.time()*1000),recvWindow=5000)
                    response=requests.get(url,params=public_params,timeout=10,allow_redirects=False)
                    if response.status_code!=200:
                        status='HTTP '+str(response.status_code)
                        delay=retry_seconds(response.headers.get('Retry-After'))
                        if response.status_code in (401,403,418,429):
                            delay=max(delay,3600 if response.status_code in (401,403,418) else 60)
                        if delay:
                            self.block(host,delay,status)
                            if client and response.status_code in (418,429): client._blocked_until=max(client._blocked_until,time.monotonic()+delay)
                        raise HealthError(status,delay)
                    data=response.json()
                if time.monotonic()-started>30: raise HealthError('STALE_RESPONSE')
                return data
            except requests.Timeout: raise HealthError('TIMEOUT') from None
            except HealthError: raise
            except requests.RequestException as exc:
                response=getattr(exc,'response',None)
                if response is not None:
                    code=response.status_code
                    delay=max(retry_seconds(response.headers.get('Retry-After')),3600 if code in (401,403,418) else 60 if code==429 else 0)
                    if delay:self.block(host,delay,'HTTP '+str(code))
                    raise HealthError('HTTP '+str(code),delay) from None
                raise HealthError('TRANSPORT_UNAVAILABLE') from None
            except RuntimeError: raise HealthError('COOLDOWN_OR_API_UNAVAILABLE') from None
            except (ValueError,TypeError): raise HealthError('INVALID_RESPONSE') from None

    def save(self,exchange,component,error=None,at=None):
        now=time.time() if at is None else at
        alert=None
        with paper.session(self.path) as db:
            ensure(db)
            if getattr(db,'is_postgres',False): db.execute('SELECT pg_advisory_xact_lock(hashtext(?))',('risk:'+exchange+':'+component,))
            else: db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM risk_checks WHERE exchange=? AND component=?',(exchange,component)).fetchone()
            if error is None:
                state='HEALTHY';failures=0;first=None;last_ok=now;status='OK';alerted=0
            else:
                first=old['first_failure'] if old and old['first_failure'] is not None else now
                failures=(old['failures'] if old else 0)+1
                last_ok=old['last_ok'] if old else None
                status=error.status
                state='CRITICAL' if failures>=3 or now-first>=300 else 'DEGRADED'
                alerted=old['alerted'] if old else 0
                if state=='CRITICAL' and not alerted:
                    # Durable at-most-once claim per incident, before Telegram.
                    alerted=1
                    alert=(f"⚠️ Exchange Risk Alert\nБиржа: {exchange}\nПроблема: {component} API unavailable\n"
                           f"HTTP/status: {status}\nПовторений: {failures}\n"
                           f"Последний успешный ответ: {time.strftime('%Y-%m-%d %H:%M:%S UTC',time.gmtime(last_ok)) if last_ok else 'ещё не подтверждён'}\n"
                           "Торговый сканер не изменён. Реальные сделки не выполнялись.")
            db.execute('''INSERT INTO risk_checks (exchange,component,state,failures,last_ok,first_failure,last_check,status,alerted)
                          VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(exchange,component) DO UPDATE SET state=excluded.state,
                          failures=excluded.failures,last_ok=excluded.last_ok,first_failure=excluded.first_failure,
                          last_check=excluded.last_check,status=excluded.status,alerted=excluded.alerted''',
                       (exchange,component,state,failures,last_ok,first,now,status,alerted))
        logging.info('Exchange Risk Monitor %s %s: %s status=%s repetitions=%s',exchange,component,state,status,failures)
        if alert:
            try:self.api.telegram(alert)
            except Exception:logging.warning('Exchange Risk notification delivery uncertain: %s %s',exchange,component)
        return {'exchange':exchange,'component':component,'state':state,'status':status,'failures':failures}

    def once(self,exchange):
        results=[]
        for component,url,params,kind in ENDPOINTS[exchange]:
            error=None
            try:
                data=self.request(exchange,url,params)
                validate(exchange,kind,data)
            except HealthError as exc:
                error=exc
                if exc.retry:
                    self.block(url.split('/')[2],exc.retry,exc.status)
                    if exchange=='BingX' and self.bingx and exc.status.startswith('RATE_LIMIT'):
                        with self.bingx._lock:
                            self.bingx._blocked_until=max(self.bingx._blocked_until,time.monotonic()+exc.retry)
            except Exception: error=HealthError('TEMPORARY_DATA_UNAVAILABLE')
            results.append(self.save(exchange,component,error))
        if exchange=='BingX' and self.bingx and self.bingx.enabled and self.bingx.credentials_ready:
            for kind in ('spot_fee','futures_fee'):
                error=None
                try:
                    data=self.bingx.request(kind,{'symbol':'BTC-USDT'} if kind=='spot_fee' else {})
                    row=data.get('commission') if kind=='futures_fee' else data
                    rate=value(row.get('takerCommissionRate'))
                    if not 0<=rate<1:raise HealthError('UNKNOWN_FEE')
                except bingx.BingXUnavailable as exc:
                    label=str(exc)
                    match=re.search(r'HTTP (\d+)',label)
                    error=HealthError('HTTP '+match.group(1) if match else 'READ_ONLY_KEY_UNAVAILABLE')
                except Exception:error=HealthError('READ_ONLY_KEY_UNAVAILABLE')
                results.append(self.save(exchange,'Read-only '+kind,error))
        return results

    def loop(self,exchange):
        while True:
            try:self.once(exchange)
            except Exception as exc:logging.warning('Exchange Risk Monitor %s storage/check unavailable (%s)',exchange,type(exc).__name__)
            time.sleep(self.interval)

    def start(self):
        for exchange in EXCHANGES:
            threading.Thread(target=self.loop,args=(exchange,),daemon=True,name='risk-'+exchange).start()
        logging.info('Independent Exchange Risk Monitor started: Binance / Gate / BingX; GET ONLY')
