"""Official MEXC GET-only market/personal-fee adapter. PAPER/VIRTUAL only."""
import json
import hashlib
import hmac
import logging
import entry_diagnostics as diagnostics
import os
import re
import threading
import time
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode
import requests

BASE = 'https://api.mexc.com'
PATHS = {'spot_symbols':'/api/v3/exchangeInfo', 'spot_tickers':'/api/v3/ticker/bookTicker',
         'spot_depth':'/api/v3/depth', 'spot_fee':'/api/v3/tradeFee',
         'contracts':'/api/v1/contract/detail/country', 'futures_tickers':'/api/v1/contract/ticker',
         'futures_depth':'/api/v1/contract/depth/{symbol}',
         'funding':'/api/v1/contract/funding_rate/{symbol}',
         'funding_history':'/api/v1/contract/funding_rate/history',
         'futures_fee':'/api/v1/private/account/tiered_fee_rate/v2'}
PRIVATE = {'spot_fee','futures_fee'}

class MEXCUnavailable(RuntimeError):
    pass


def number(value, minimum=Decimal(0), strict=False):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < minimum or (strict and result == minimum): raise ValueError()
        return result
    except (ValueError, InvalidOperation, TypeError):
        raise MEXCUnavailable('MEXC required numeric value UNKNOWN') from None


def pair(symbol):
    if not isinstance(symbol,str) or not re.fullmatch(r'[A-Z0-9]+USDT',symbol):
        raise MEXCUnavailable('MEXC symbol mapping UNKNOWN')
    return symbol[:-4]+'_USDT'


class Client:
    def __init__(self):
        self.key=os.getenv('MEXC_API_KEY','').strip()
        self.secret=os.getenv('MEXC_API_SECRET','').strip()
        self.enabled=os.getenv('MEXC_ENABLED','true').lower()=='true'
        self._lock=threading.RLock()
        self._next=0
        self._blocked=0
        self._cache={}
        self.auth={'spot':False,'futures':False}
        self.report={}
        self.last_status={}

    @property
    def credentials_ready(self):
        return bool(self.key and self.secret)

    def cached(self,key,ttl,load):
        with self._lock:
            old=self._cache.get(key)
            if old and old[0]>time.monotonic(): return old[1]
            result=load()
            self._cache[key]=(time.monotonic()+ttl,result)
            return result

    def request(self,kind,params=None):
        if kind not in PATHS: raise MEXCUnavailable('MEXC endpoint outside GET allowlist')
        if not self.enabled: raise MEXCUnavailable('MEXC disabled')
        if kind in PRIVATE and not self.credentials_ready: raise MEXCUnavailable('MEXC credentials missing')
        business=dict(params or {})
        allowed=('symbol','page_num','page_size') if kind=='funding_history' else ('symbol','limit')
        if any(k not in allowed or not re.fullmatch(r'[A-Za-z0-9_]+',str(v)) for k,v in business.items()):
            raise MEXCUnavailable('MEXC invalid parameters')
        path=PATHS[kind]
        if '{symbol}' in path:
            if not re.fullmatch(r'[A-Z0-9]+_USDT',business.get('symbol','')): raise MEXCUnavailable('MEXC contract mapping UNKNOWN')
            path=path.format(symbol=business.pop('symbol'))
        with self._lock:
            if time.monotonic()<self._blocked: raise MEXCUnavailable('MEXC cooldown active')
            wait=max(0,self._next-time.monotonic())
            if wait: time.sleep(wait)
            # Separate, sequential 1 request/s policy, below documented endpoint limits.
            self._next=time.monotonic()+1.05
            headers={}
            if kind in PRIVATE:
                stamp=str(int(time.time()*1000))
                if kind=='spot_fee':
                    business.update(timestamp=stamp,recvWindow=5000)
                    canonical=urlencode(sorted(business.items()))
                    business=dict(sorted(business.items()))
                    business['signature']=hmac.new(self.secret.encode(),canonical.encode(),hashlib.sha256).hexdigest()
                    headers={'X-MEXC-APIKEY':self.key}
                else:
                    business=dict(sorted(business.items()))
                    canonical=urlencode(business)
                    signature=hmac.new(self.secret.encode(),(self.key+stamp+canonical).encode(),hashlib.sha256).hexdigest()
                    headers={'ApiKey':self.key,'Request-Time':stamp,'Signature':signature,'Recv-Window':'10','Language':'en-US'}
            try:
                base='https://contract.mexc.com' if kind=='funding_history' else BASE
                r=requests.get(base+path,params=business,headers=headers,timeout=10,allow_redirects=False)
                try: payload=r.json()
                except ValueError: payload=None
                code=payload.get('code') if isinstance(payload,dict) else None
                safe_code=code if type(code) is int else 'UNKNOWN'
                self.last_status[kind]={'http_status':r.status_code,'code':safe_code}
                logging.info('MEXC GET-only response: endpoint=%s HTTP=%s code=%s',PATHS[kind],r.status_code,safe_code)
                if r.status_code in (418,429,401,403) or safe_code in (429,10072,602,401,403):
                    retry=r.headers.get('Retry-After')
                    try: seconds=float(retry)
                    except (ValueError,TypeError):
                        try: seconds=parsedate_to_datetime(retry).timestamp()-time.time()
                        except (ValueError,TypeError,OverflowError): seconds=3600 if r.status_code in (418,401,403) else 60
                    if not 0<=seconds<float('inf'): seconds=60
                    self._blocked=time.monotonic()+seconds
                if r.status_code!=200: raise MEXCUnavailable(f'MEXC {kind} HTTP {r.status_code}')
                if isinstance(payload,dict) and payload.get('code') not in (None,0):
                    raise MEXCUnavailable(f'MEXC {kind} API rejected request code={safe_code}')
                if kind.startswith('futures') or kind in ('contracts','funding','funding_history'):
                    if not isinstance(payload,dict) or payload.get('success') is not True or payload.get('code')!=0:
                        raise MEXCUnavailable('MEXC Futures response UNKNOWN')
                    payload=payload.get('data')
                elif kind=='spot_fee':
                    if not isinstance(payload,dict) or payload.get('code')!=0: raise MEXCUnavailable('MEXC fee response UNKNOWN')
                    payload=payload.get('data')
                if payload is None: raise MEXCUnavailable('MEXC data UNKNOWN')
                # HTTP Date is retained only for public book freshness validation.
                if kind=='spot_depth' and isinstance(payload,dict):
                    try: payload['_http_date']=parsedate_to_datetime(r.headers.get('Date')).timestamp()
                    except (TypeError,ValueError,OverflowError): payload['_http_date']=None
                return payload
            except requests.RequestException:
                raise MEXCUnavailable(f'MEXC {kind} transport unavailable') from None

    def spot_symbols(self):
        data=self.cached('spot_symbols',300,lambda:self.request('spot_symbols'))
        if not isinstance(data,dict) or not isinstance(data.get('symbols'),list): raise MEXCUnavailable('MEXC spot symbols UNKNOWN')
        return {r['symbol']:r for r in data['symbols'] if isinstance(r,dict) and
                r.get('quoteAsset')=='USDT' and r.get('symbol')==str(r.get('baseAsset'))+'USDT' and
                r.get('status') in ('1',1,'ENABLED') and r.get('isSpotTradingAllowed') is True}

    def contracts(self):
        data=self.cached('contracts',300,lambda:self.request('contracts'))
        rows=data if isinstance(data,list) else [data]
        return {r['symbol'].replace('_',''):r for r in rows if isinstance(r,dict) and
                r.get('symbol')==str(r.get('baseCoin'))+'_USDT' and r.get('quoteCoin')=='USDT' and
                r.get('settleCoin')=='USDT' and r.get('state')==0 and r.get('futureType')==1 and r.get('apiAllowed') is True}

    def tickers(self,futures=False):
        allowed=self.contracts() if futures else self.spot_symbols()
        data=self.request('futures_tickers' if futures else 'spot_tickers')
        rows=data if isinstance(data,list) else [data]
        result={}
        for r in rows:
            if not isinstance(r,dict): continue
            symbol=r.get('symbol','').replace('_','')
            if symbol not in allowed: continue
            try:
                ask=number(r.get('ask1' if futures else 'askPrice'),strict=True)
                bid=number(r.get('bid1' if futures else 'bidPrice'),strict=True)
                if futures: self.fresh_timestamp(r.get('timestamp'))
                if bid<=ask: result[symbol]={'askPrice':str(ask),'bidPrice':str(bid)}
            except MEXCUnavailable: continue
        return result

    @staticmethod
    def fresh_timestamp(ms):
        age=time.time()-float(number(ms,strict=True)/1000)
        if not -5<=age<=30: raise MEXCUnavailable('MEXC stale snapshot')

    def spot_rules(self,symbol,side='buy'):
        r = None
        try:
            r = self.spot_symbols().get(symbol)
            return self._spot_rules(symbol, side, r)
        except Exception as exc:
            labels = getattr(exc, 'missing_mandatory_data', [])
            if 'MISSING_MIN_QTY' in labels or str(exc) == 'MEXC spot side unavailable':
                self.spot_metadata_snapshot(symbol, r, 'MISSING_MIN_QTY' if 'MISSING_MIN_QTY' in labels else 'MISSING_SPOT_MARKET_PARAMS')
            raise

    @staticmethod
    def spot_metadata_snapshot(symbol, row, reject_reason):
        cycle = diagnostics.CYCLE.get()
        if cycle is None: return
        seen = cycle.setdefault('mexc_spot_snapshot_symbols', set())
        if symbol in seen: return
        seen.add(symbol)
        row = row if isinstance(row, dict) else {}
        raw = row.get('baseSizePrecision')
        present = 'baseSizePrecision' in row
        parsed = diagnostics.numeric(raw)
        try:
            number(raw, strict=True)
            result = 'ACCEPTED'
        except MEXCUnavailable:
            cause = 'FIELD_ABSENT' if not present else 'FIELD_NULL' if raw is None else 'FIELD_EMPTY' if raw == '' else 'FIELD_ZERO' if parsed != 'UNKNOWN' and Decimal(parsed) == 0 else 'FIELD_INVALID'
            result = 'REJECTED_' + cause
        def public_value(key):
            if key not in row: return 'UNKNOWN'
            value = row[key]
            if value is None or type(value) in (bool, int): return value
            if type(value) is float: return diagnostics.numeric(value)
            if isinstance(value, str) and len(value) <= 64 and (re.fullmatch(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?',value) or value in ('','ENABLED','DISABLED','NaN','Infinity','-Infinity')): return value
            return 'INVALID_FORMAT_REDACTED'
        fields = ('status','isSpotTradingAllowed','tradeSideType','baseAssetPrecision','baseSizePrecision','quoteAmountPrecision','quoteAmountPrecisionMarket')
        snapshot = {key:public_value(key) for key in fields}
        snapshot.update(symbol=diagnostics.safe(symbol), raw_type_baseSizePrecision=type(raw).__name__ if present else 'MISSING', parsed_baseSizePrecision=parsed, min_qty_validation_result=result, reject_reason=reject_reason)
        logging.info('MEXC Spot metadata diagnostic: %s', json.dumps(snapshot,sort_keys=True))

    def _spot_rules(self,symbol,side,r):
        if not r or r.get('tradeSideType') not in ('1',1,'2' if side=='buy' else '3'):
            raise MEXCUnavailable('MEXC spot side unavailable')
        precision=r.get('baseAssetPrecision')
        if type(precision) is not int or not 0<=precision<=18: raise MEXCUnavailable('MEXC quantity precision UNKNOWN')
        # No documented Spot increment source has been confirmed. Precision is
        # not evidence of a quantity increment; do not synthesize 10**-precision.
        quantity_rules = self.validate_spot_quantity_rules(r.get('baseSizePrecision'), None)
        return {**quantity_rules,
                'min_quote':diagnostics.call('MISSING_MIN_NOTIONAL', number, r.get('quoteAmountPrecision'), strict=True),
                'max_qty':None,'max_quote':number(r['maxQuoteAmount'],strict=True) if r.get('maxQuoteAmount') is not None else None,
                'market_max_qty':None,'market_max_quote':None}

    @staticmethod
    def validate_spot_quantity_rules(minimum, confirmed_increment):
        """Validate values, never infer them. Production has no increment source.

        confirmed_increment may only be supplied by a verified official source;
        no new metadata field, environment fallback or per-symbol guess is used.
        """
        values, missing, reasons = {}, [], []
        for key, value, category, reason in (
            ('min_qty', minimum, 'MISSING_MIN_QTY', 'MEXC_SPOT_UNKNOWN_MIN_QTY'),
            ('step', confirmed_increment, 'MISSING_STEP_SIZE', 'MEXC_SPOT_UNKNOWN_QUANTITY_INCREMENT'),
        ):
            try: values[key] = number(value, strict=True)
            except MEXCUnavailable:
                missing.append(category); reasons.append(reason)
        if missing:
            exc = MEXCUnavailable('MEXC Spot quantity rules UNKNOWN')
            exc.missing_mandatory_data = missing
            exc.mexc_spot_quantity_reasons = reasons
            raise exc
        return values

    def futures_meta(self,symbol):
        r=self.contracts().get(symbol)
        if not r: raise MEXCUnavailable('MEXC perpetual mapping UNKNOWN')
        multiplier=number(r.get('contractSize'),strict=True)
        return {'step':diagnostics.call('MISSING_STEP_SIZE', number, r.get('volUnit'), strict=True)*multiplier,
                'min_qty':diagnostics.call('MISSING_MIN_QTY', number, r.get('minVol'), strict=True)*multiplier,
                # MEXC documents a minimum contract volume, not a separate minNotional.
                'min_notional':Decimal(0),'multiplier':multiplier,'funding':None}

    def orderbook(self,symbol,futures=False):
        if futures: multiplier=self.futures_meta(symbol)['multiplier']
        else: diagnostics.call('MISSING_SPOT_MARKET_PARAMS', self.spot_rules, symbol); multiplier=Decimal(1)
        r=self.request('futures_depth' if futures else 'spot_depth',{'symbol':pair(symbol) if futures else symbol,'limit':100})
        if not isinstance(r,dict): raise MEXCUnavailable('MEXC depth UNKNOWN')
        if futures: self.fresh_timestamp(r.get('timestamp'))
        elif r.get('_http_date') is None or not -5<=time.time()-r['_http_date']<=30:
            raise MEXCUnavailable('MEXC Spot book freshness UNKNOWN')
        books=[]
        for side in ('asks','bids'):
            rows=r.get(side)
            if not isinstance(rows,list) or not rows: raise MEXCUnavailable('MEXC depth UNKNOWN')
            levels=[]
            for x in rows:
                if not isinstance(x,(list,tuple)) or len(x)<2: raise MEXCUnavailable('MEXC malformed level')
                levels.append([number(x[0],strict=True),number(x[1],strict=True)*multiplier])
            levels.sort(key=lambda x:x[0],reverse=side=='bids')
            if len(set(x[0] for x in levels))!=len(levels): raise MEXCUnavailable('MEXC duplicate level')
            books.append(levels)
        if books[1][0][0]>books[0][0][0]: raise MEXCUnavailable('MEXC crossed depth')
        return tuple(books)

    def fee_rates(self,symbol,futures=False):
        def load():
            r=self.request('futures_fee' if futures else 'spot_fee',{'symbol':pair(symbol) if futures else symbol})
            if not isinstance(r,dict): raise MEXCUnavailable('MEXC personal fee UNKNOWN')
            if futures and (r.get('symbol')!=pair(symbol) or r.get('feeRateMode') not in ('NORMAL','TIERED')):
                raise MEXCUnavailable('MEXC applicable personal fee UNKNOWN')
            maker=number(r.get('realMakerFee' if futures else 'makerCommission'))
            taker=number(r.get('realTakerFee' if futures else 'takerCommission'))
            if max(maker,taker)>=1: raise MEXCUnavailable('MEXC invalid fee')
            return {'maker':maker,'taker':taker}
        return self.cached(('fee',futures,symbol),3600,load)

    def fee(self,symbol,futures=False):
        return self.fee_rates(symbol,futures)['taker']

    def fresh_funding(self,symbol):
        r=self.request('funding',{'symbol':pair(symbol)})
        if not isinstance(r,dict) or r.get('symbol')!=pair(symbol): raise MEXCUnavailable('MEXC funding UNKNOWN')
        self.fresh_timestamp(r.get('timestamp'))
        rate=number(r.get('fundingRate'),minimum=Decimal('-0.1'))
        at=float(diagnostics.call('MISSING_FUNDING_TIMESTAMP', number, r.get('nextSettleTime'), strict=True)/1000)
        if abs(rate)>Decimal('0.1') or at<=time.time(): raise MEXCUnavailable('MEXC funding UNKNOWN')
        return rate,at

    def probe(self):
        report={'mexc_credentials_present':self.credentials_ready,'mexc_auth_test_http_status':'UNKNOWN','mexc_auth_ok':False}
        if not self.enabled or not self.credentials_ready:
            logging.info('MEXC READ-ONLY readiness: %s',report);self.report=report;return report
        # Credentials tested first; market verification does not create episodes.
        for futures in (False,True):
            name='futures' if futures else 'spot'
            try:
                self.fee_rates('BTCUSDT',futures)
                self.auth[name]=True;report['mexc_'+name+'_fee']='REAL'
            except (MEXCUnavailable, TypeError, ValueError, KeyError):
                report['mexc_'+name+'_fee']='UNKNOWN'
            status=self.last_status.get(name+'_fee',{}).get('http_status','UNKNOWN')
            if not futures: report['mexc_auth_test_http_status']=status
            report['mexc_'+name+'_auth_http_status']=status
        report['mexc_auth_ok']=any(self.auth.values())
        for name,load in [('spot_market',lambda:self.tickers()),('spot_depth',lambda:self.orderbook('BTCUSDT')),
                          ('futures_market',lambda:self.tickers(True)),('futures_depth',lambda:self.orderbook('BTCUSDT',True)),
                          ('funding',lambda:self.fresh_funding('BTCUSDT'))]:
            try: report['mexc_'+name]=bool(load())
            except (MEXCUnavailable, TypeError, ValueError, KeyError): report['mexc_'+name]=False
        self.report=report
        logging.info('MEXC READ-ONLY readiness: %s',report)
        return report
