"""All exchange responses are mocked: tests send no Binance requests."""
import unittest
from collections import deque
from decimal import Decimal
from unittest.mock import Mock, patch
import requests
import app
import basis
import binance_io


class MinimalBinanceTests(unittest.TestCase):
    def setUp(self):
        from contextlib import ExitStack
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(app.cache,{},clear=True))
        self.stack.enter_context(patch.dict(app.api_blocked_until,{},clear=True))
        self.stack.enter_context(patch.dict(basis.api_blocked_until,{},clear=True))
        self.stack.enter_context(patch.dict(binance_io.snapshots,{},clear=True))
        self.stack.enter_context(patch.object(binance_io,'request_history',deque()))
        self.stack.enter_context(patch.dict(binance_io.weight_headers,{},clear=True))
        self.stack.enter_context(patch.object(binance_io,'next_request',0))
        self.stack.enter_context(patch.object(binance_io,'last_clock',0))
        self.stack.enter_context(patch.object(app.paper,'load_api_backoff',return_value=0))
        self.save=self.stack.enter_context(patch.object(app.paper,'save_api_backoff'))
        self.stack.enter_context(patch.object(app,'binance_signed_params',side_effect=lambda params:params))

    def test_commission_symbol_cache_survives_3599s_and_refreshes_on_expiry(self):
        with patch.object(app.time,'monotonic',return_value=100) as clock, patch.object(app,'get_json',side_effect=[{'takerCommissionRate':'0.0003'},{'takerCommissionRate':'0.0007'},{'takerCommissionRate':'0.0004'}]) as get:
            self.assertEqual(basis.futures_fee(app,'Binance','AAAUSDT'),Decimal('0.0003'))
            self.assertEqual(basis.futures_fee(app,'Binance','BBBUSDT'),Decimal('0.0007'))
            clock.return_value=3699
            self.assertEqual(basis.futures_fee(app,'Binance','AAAUSDT'),Decimal('0.0003'))
            self.assertEqual(get.call_count,2)
            clock.return_value=3700
            self.assertEqual(basis.futures_fee(app,'Binance','AAAUSDT'),Decimal('0.0004'))
            self.assertEqual(get.call_count,3)
        self.assertEqual(get.call_args.kwargs['params']['symbol'],'AAAUSDT')

    def test_expired_unknown_fee_blocks_evaluation_and_is_not_replaced_by_zero(self):
        with patch.object(app.time,'monotonic',return_value=100) as clock, patch.object(app,'get_json',side_effect=[{'takerCommissionRate':'0.0003'},{}]):
            basis.futures_fee(app,'Binance','AAAUSDT')
            clock.return_value=3700
            with patch.object(basis,'futures_meta',return_value={}),patch.object(app,'fee',return_value=Decimal('0.001')):
                with self.assertRaisesRegex(ValueError,'Personal futures fee unavailable'):
                    basis.evaluate(app,'AAAUSDT','Gate','Binance')
            self.assertLessEqual(app.cache[('basis-futures-fee','Binance','AAAUSDT')][0],clock.return_value)

    def test_gate_fee_ttl_is_unchanged(self):
        with patch.object(app,'cached',return_value=Decimal('0.001')) as cached:
            basis.futures_fee(app,'Gate','AAAUSDT')
        self.assertEqual(cached.call_args.args[1],60)

    def test_false_error_response_retains_retry_after_in_basis(self):
        response=requests.Response();response.status_code=429
        response.headers['Retry-After']='15';response.url='https://fapi.binance.com/fapi/v1/commissionRate'
        self.assertFalse(response)
        api=Mock(multi_exchange=True)
        api.market_maps.return_value=({'Gate':{'AAAUSDT':{'lowest_ask':'1'}}},{'Binance':{'AAAUSDT':{'bidPrice':'2'}}})
        api.credentials_ready.return_value=True;api.dec=app.dec
        with patch.object(basis.paper,'storage_ready',return_value=True),patch.object(basis,'evaluate',side_effect=requests.HTTPError(response=response)),patch('entry_policy.reject'),patch.object(basis.time,'time',return_value=100):
            basis.scan(api)
        self.assertEqual(basis.api_blocked_until['Binance'],115)

    def test_http_helper_honors_15s_persists_and_keeps_hosts_separate(self):
        bad=requests.Response();bad.status_code=429;bad.headers['Retry-After']='15'
        bad._content=b'{"code":-1003}'
        good=requests.Response();good.status_code=200;good._content=b'{}'
        with patch.object(app.time,'monotonic',return_value=100) as clock,patch.object(app.time,'time',return_value=1000),patch.object(app.requests,'get',side_effect=[bad,good,good]) as get:
            with self.assertRaises(requests.HTTPError):
                app.get_json('https://fapi.binance.com/fapi/v1/depth',params={'symbol':'AAAUSDT','limit':50})
            self.assertEqual(app.api_blocked_until['fapi.binance.com'],115)
            self.assertEqual(self.save.call_args.args,('fapi.binance.com',1015))
            clock.return_value=114
            with self.assertRaises(RuntimeError):app.get_json('https://fapi.binance.com/fapi/v1/depth')
            self.assertEqual(len(binance_io.request_history),1)
            app.get_json('https://api.binance.com/api/v3/ticker/bookTicker')
            clock.return_value=115
            app.get_json('https://fapi.binance.com/fapi/v1/depth')
        self.assertEqual(get.call_count,3)

    def test_rolling_60_seconds_expires_old_requests_and_counts_private_weight(self):
        with patch.object(binance_io.time,'monotonic',return_value=100):
            binance_io.request_started('api.binance.com','/api/v3/depth',{'limit':100})
        with patch.object(binance_io.time,'monotonic',return_value=159):
            binance_io.request_started('fapi.binance.com','/fapi/v1/commissionRate',{'symbol':'AAAUSDT'})
            self.assertEqual(binance_io.rolling_load(),{'requests':2,'estimated_weight':25})
        self.assertEqual(binance_io.rolling_load(160),{'requests':1,'estimated_weight':20})
        self.assertEqual(binance_io.rolling_load(220),{'requests':0,'estimated_weight':0})

    def test_weight_headers_and_rate_limit_log_redact_secrets(self):
        response=Mock(status_code=429,headers={'X-MBX-USED-WEIGHT-1M':'234','X-MBX-USED-WEIGHT':'123','X-MBX-USED-WEIGHT-5M':'SECRET','X-MBX-APIKEY':'SECRET','Authorization':'SECRET'})
        with patch.object(binance_io.time,'monotonic',return_value=100):
            binance_io.request_started('fapi.binance.com','/fapi/v1/depth',{'limit':50,'signature':'SECRET'})
            with self.assertLogs(level='WARNING') as logs:
                binance_io.response_observed('fapi.binance.com','/fapi/v1/depth',response)
        text='\n'.join(logs.output)
        self.assertIn('requests_last_60s_before_429=1',text)
        self.assertIn('estimated_weight_last_60s_before_429=2',text)
        self.assertIn('234',text);self.assertNotIn('SECRET',text)
        self.assertEqual(binance_io.weight_headers['fapi.binance.com']['values'],{'x-mbx-used-weight-1m':234,'x-mbx-used-weight':123})

    def test_cache_hits_not_counted_as_actual_requests(self):
        response=Mock(status_code=200,headers={});response.json.return_value={}
        with patch.object(app.time,'monotonic',return_value=100),patch.object(app.requests,'get',return_value=response) as get:
            app.get_json('https://fapi.binance.com/fapi/v1/depth',params={'limit':50})
            app.get_json('https://fapi.binance.com/fapi/v1/depth',params={'limit':50})
            self.assertEqual(binance_io.rolling_load()['requests'],1)
        get.assert_called_once()

    def test_retry_after_dates_and_fallback_only_when_missing(self):
        with patch.object(binance_io.time,'time',return_value=0):
            self.assertEqual(binance_io.cooldown_seconds(418,'Thu, 01 Jan 1970 00:02:00 GMT'),120)
        self.assertEqual(binance_io.cooldown_seconds(418,'15'),15)
        self.assertEqual(binance_io.cooldown_seconds(429,'15'),15)
        self.assertEqual(binance_io.cooldown_seconds(429,''),60)
        self.assertEqual(binance_io.cooldown_seconds(418,''),3600)

    def test_monitor_uses_same_binance_retry_after_without_60s_override(self):
        import risk_monitor
        api=Mock()
        response=requests.Response();response.status_code=429
        response.headers['Retry-After']='15'
        api.get_json.side_effect=requests.HTTPError(response=response)
        monitor=risk_monitor.Monitor(api,'/tmp/unused-monitor-test.sqlite')
        with patch.object(monitor,'blocked'),patch.object(monitor,'block') as block:
            with self.assertRaises(risk_monitor.HealthError):
                monitor.request('Binance','https://fapi.binance.com/fapi/v1/depth',{'symbol':'BTCUSDT','limit':5})
        block.assert_called_once_with('fapi.binance.com',15,'HTTP 429')
