import hashlib
import hmac
import time
import unittest
from decimal import Decimal
from unittest.mock import Mock, patch
from urllib.parse import urlencode
import mexc
import exchanges


def client():
    with patch.dict(mexc.os.environ,{'MEXC_API_KEY':'test-key','MEXC_API_SECRET':'test-secret','MEXC_ENABLED':'true'}):
        return mexc.Client()

def response(data,futures=False,status=200,headers=None):
    r=Mock(status_code=status,headers=headers or {})
    r.json.return_value={'success':True,'code':0,'data':data} if futures else data
    return r

class MEXCTests(unittest.TestCase):
    def test_get_allowlist_and_parameter_validation(self):
        c=client()
        with patch.object(mexc.requests,'get') as get:
            for name in ('order','cancel','withdrawal','transfer'):
                with self.assertRaises(mexc.MEXCUnavailable):c.request(name)
            with self.assertRaises(mexc.MEXCUnavailable):c.request('spot_fee',{'symbol':'BTCUSDT&signature=BAD'})
        get.assert_not_called()

    def test_spot_signature_matches_actual_query(self):
        c=client()
        with patch.object(mexc.requests,'get',return_value=response({'code':0,'data':{'makerCommission':'0','takerCommission':'0.001'}})) as get:
            c.fee_rates('BTCUSDT')
        kw=get.call_args.kwargs;query=dict(kw['params']);signature=query.pop('signature')
        self.assertEqual(signature,hmac.new(b'test-secret',urlencode(query).encode(),hashlib.sha256).hexdigest())
        self.assertEqual(kw['headers']['X-MEXC-APIKEY'],'test-key')
        self.assertFalse(kw['allow_redirects'])

    def test_futures_signature_matches_actual_query(self):
        c=client()
        with patch.object(mexc.requests,'get',return_value=response({'symbol':'BTC_USDT','feeRateMode':'NORMAL','realMakerFee':'0.0001','realTakerFee':'0.0005'},True)) as get:
            c.fee_rates('BTCUSDT',True)
        kw=get.call_args.kwargs;h=kw['headers']
        expected=hmac.new(b'test-secret',('test-key'+h['Request-Time']+urlencode(kw['params'])).encode(),hashlib.sha256).hexdigest()
        self.assertEqual(h['Signature'],expected)

    def test_unknown_fees_and_leverage_mode_block(self):
        c=client()
        for payload,market in [({},False),({'makerCommission':'0'},False),({'symbol':'BTC_USDT','feeRateMode':'LEVERAGE','realMakerFee':0,'realTakerFee':0},True)]:
            with patch.object(c,'request',return_value=payload):
                with self.assertRaises(mexc.MEXCUnavailable):c.fee_rates('BTCUSDT',market)

    def test_per_symbol_cache_and_expiry_fail_closed(self):
        c=client()
        with patch.object(c,'request',return_value={'makerCommission':0,'takerCommission':'0.001'}) as get:
            c.fee('BTCUSDT');c.fee('BTCUSDT');c.fee('ETHUSDT')
            self.assertEqual(get.call_count,2)
        c._cache[('fee',False,'BTCUSDT')]=(0,{'maker':Decimal(0),'taker':Decimal('.001')})
        with patch.object(c,'request',side_effect=mexc.MEXCUnavailable('unknown')):
            with self.assertRaises(mexc.MEXCUnavailable):c.fee('BTCUSDT')

    def test_retry_after_blocks_next_http(self):
        c=client()
        with patch.object(mexc.requests,'get',return_value=response({},status=429,headers={'Retry-After':'419'})) as get:
            with self.assertRaises(mexc.MEXCUnavailable):c.request('spot_symbols')
            with self.assertRaises(mexc.MEXCUnavailable):c.request('contracts')
            self.assertEqual(get.call_count,1)
        self.assertGreater(c._blocked,time.monotonic()+418)

    def test_safe_transport_error(self):
        c=client()
        with patch.object(mexc.requests,'get',side_effect=mexc.requests.RequestException('signature=test-secret')):
            with self.assertRaises(mexc.MEXCUnavailable) as exc:c.request('spot_symbols')
        self.assertNotIn('secret',str(exc.exception))

    def test_contract_mapping_and_minimum(self):
        c=client();r={'symbol':'BTC_USDT','baseCoin':'BTC','quoteCoin':'USDT','settleCoin':'USDT','futureType':1,'state':0,'apiAllowed':True,'contractSize':'0.001','minVol':2,'volUnit':1}
        with patch.object(c,'request',return_value=[r,{**r,'symbol':'1000BTC_USDT'}, {**r,'symbol':'ETH_USDT','futureType':2}]):
            self.assertEqual(set(c.contracts()),{'BTCUSDT'})
        meta=c.futures_meta('BTCUSDT')
        self.assertEqual(meta['min_qty'],Decimal('.002'))
        self.assertEqual(meta['step'],Decimal('.001'))

    def test_depth_contract_quantities_and_stale(self):
        c=client();row={'timestamp':int(time.time()*1000),'asks':[[101,10,2]],'bids':[[100,20,1]]}
        with patch.object(c,'futures_meta',return_value={'multiplier':Decimal('.01')}),patch.object(c,'request',return_value=row):
            asks,bids=c.orderbook('BTCUSDT',True)
            self.assertEqual(asks[0][1],Decimal('.10'))
            self.assertEqual(bids[0][1],Decimal('.20'))
            row['timestamp']=int((time.time()-60)*1000)
            with self.assertRaises(mexc.MEXCUnavailable):c.orderbook('BTCUSDT',True)

    def test_spot_unknown_freshness_blocks(self):
        c=client()
        with patch.object(c,'spot_rules',return_value={}),patch.object(c,'request',return_value={'asks':[['101','1']],'bids':[['100','1']]}):
            with self.assertRaises(mexc.MEXCUnavailable):c.orderbook('BTCUSDT')

    def test_funding_requires_real_next_timestamp(self):
        c=client();r={'symbol':'BTC_USDT','timestamp':int(time.time()*1000),'fundingRate':'0.0001','nextSettleTime':int((time.time()+3600)*1000)}
        with patch.object(c,'request',return_value=r):
            self.assertEqual(c.fresh_funding('BTCUSDT')[0],Decimal('.0001'))
            r.pop('nextSettleTime')
            with self.assertRaises(mexc.MEXCUnavailable):c.fresh_funding('BTCUSDT')

    def test_routing_preserves_legacy(self):
        old=Mock();c=client();bx=Mock();api=exchanges.ScannerAPI(old,bx,c)
        with patch.object(c,'fee',return_value=Decimal('.001')) as fee:
            self.assertEqual(api.fee('MEXC','BTCUSDT'),Decimal('.001'))
            fee.assert_called_once_with('BTCUSDT')
        api.orderbook('Gate','BTCUSDT');old.orderbook.assert_called_once_with('Gate','BTCUSDT')

    def test_missing_credentials_no_http_probe(self):
        c=client();c.secret=''
        with patch.object(mexc.requests,'get') as get:
            self.assertFalse(c.probe()['mexc_credentials_present'])
        get.assert_not_called()
