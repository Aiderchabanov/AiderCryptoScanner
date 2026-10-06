import json
import time
import unittest
from unittest.mock import Mock, patch
import requests
import bingx
import bingx_diagnostics as d
from test_bingx import client, response


class BingXDiagnosticTests(unittest.TestCase):
    def failure(self, operation, reply=None, error=None):
        c = client()
        with patch.object(bingx.requests,'get',return_value=reply,side_effect=error) as get:
            with self.assertRaises(bingx.BingXUnavailable) as caught:
                operation(c)
            get.assert_called_once()
        self.assertIs(type(caught.exception),bingx.BingXUnavailable)
        self.assertIsNone(d.CONTEXT.get())
        return caught.exception.diagnostic_context

    def test_success_identical_payload_and_one_request(self):
        c=client(); payload={'x':1}
        with patch.object(bingx.requests,'get',return_value=response(payload)) as get:
            self.assertEqual(c.request('contracts'),payload)
            get.assert_called_once()
        self.assertIsNone(d.CONTEXT.get())

    def test_http_error_has_status_code_message(self):
        r=response(None,503,100001);r.json.return_value['msg']='Temporary unavailable'
        q=self.failure(lambda c:c.orderbook('PUMPBTCUSDT',True),r)
        self.assertEqual((q['http_status'],q['api_code'],q['api_message']),(503,100001,'Temporary unavailable'))
        self.assertEqual((q['market'],q['operation'],q['endpoint_class'],q['symbol']),('Futures','orderbook','futures_depth','PUMPBTC-USDT'))

    def test_api_error_and_existing_cooldown_unchanged(self):
        c=client();r=response({},200,100410);r.json.return_value['msg']='rate limit'
        with patch.object(bingx.requests,'get',return_value=r) as get:
            with self.assertRaises(bingx.BingXUnavailable) as e:c.fresh_funding('PUMPBTCUSDT')
            self.assertEqual(e.exception.diagnostic_context['api_code'],100410)
            with self.assertRaises(bingx.BingXUnavailable) as e:c.orderbook('PUMPBTCUSDT',True)
            self.assertTrue(e.exception.diagnostic_context['cooldown_active'])
            get.assert_called_once()

    def test_missing_response_data(self):
        q=self.failure(lambda c:c.request('contracts'),response(None))
        self.assertTrue(q['missing_data'])
        self.assertEqual(q['endpoint_class'],'contracts')

    def test_timeout_saves_type_not_sensitive_exception_text(self):
        q=self.failure(lambda c:c.orderbook('PUMPBTCUSDT',True),error=requests.Timeout('secret=test-secret https://host/?signature=hidden'))
        self.assertTrue(q['timeout']);self.assertEqual(q['transport_error'],'Timeout')
        self.assertNotIn('hidden',json.dumps(q));self.assertNotIn('test-secret',json.dumps(q))

    def test_transport_error(self):
        q=self.failure(lambda c:c.request('contracts'),error=requests.ConnectionError('private URL'))
        self.assertFalse(q['timeout']);self.assertEqual(q['transport_error'],'ConnectionError')

    def test_invalid_json(self):
        r=response({});r.json.side_effect=ValueError('HTML credentials here')
        q=self.failure(lambda c:c.request('contracts'),r)
        self.assertEqual(q['http_status'],200);self.assertEqual(q['json_error'],'ValueError')
        self.assertNotIn('credentials here',json.dumps(q))

    def test_stale_book_preserves_success_http_context(self):
        q=self.failure(lambda c:c.orderbook('PUMPBTCUSDT',True),response({'T':(time.time()-60)*1000,'asks':[['2','1']],'bids':[['1','1']]}))
        self.assertTrue(q['stale_data']);self.assertEqual(q['http_status'],200)
        self.assertEqual(q['reason'],'BingX stale orderbook')

    def test_missing_fee(self):
        q=self.failure(lambda c:c.fee('PUMPBTCUSDT',True),response({}))
        self.assertTrue(q['missing_data']);self.assertEqual(q['operation'],'fee')
        self.assertEqual(q['endpoint_class'],'futures_fee')

    def test_missing_funding(self):
        q=self.failure(lambda c:c.fresh_funding('PUMPBTCUSDT'),response({}))
        self.assertTrue(q['missing_data']);self.assertEqual(q['operation'],'funding')
        self.assertEqual(q['endpoint_class'],'funding')

    def test_metadata_and_mapping_failure_without_network(self):
        q=self.failure(lambda c:c.futures_meta('PUMPBTCUSDT'),response([]))
        self.assertEqual(q['operation'],'market_metadata');self.assertEqual(q['endpoint_class'],'contracts')
        with patch.object(bingx.requests,'get') as get:
            with self.assertRaises(bingx.BingXUnavailable) as e:bingx.pair('BTCUSDT&secret=test-secret')
            get.assert_not_called()
        self.assertEqual(e.exception.diagnostic_context['operation'],'symbol_mapping')
        self.assertEqual(e.exception.diagnostic_context['symbol'],'UNKNOWN')

    def test_secrets_and_signed_query_never_logged(self):
        r=response({},200,123);r.json.return_value['msg']='test-key test-secret signature=123abc API_KEY=test-key https://host/path?timestamp=123&signature=abcd'
        c=client()
        with patch.object(bingx.requests,'get',return_value=r),self.assertLogs(level='WARNING') as log:
            with self.assertRaises(bingx.BingXUnavailable) as e:c.orderbook('PUMPBTCUSDT',True)
            d.emit(e.exception,21,'PUMPBTCUSDT','observation')
        value=' '.join(log.output)
        for secret in ('test-key','test-secret','123abc','https://','timestamp=','signature=','API_KEY='):
            self.assertNotIn(secret,value)
        self.assertIn('"episode_id": 21',value)

    def test_worker_log_has_no_requests_or_episode_mutation(self):
        episode={'id':21,'symbol':'PUMPBTCUSDT'}; original=dict(episode)
        exc=bingx.BingXUnavailable('safe')
        with patch.object(bingx.requests,'get') as get,self.assertLogs(level='WARNING') as log:
            for worker in ('observation','checkpoint','funding_warning'):
                d.emit(exc,episode['id'],episode['symbol'],worker)
            get.assert_not_called()
        self.assertEqual(episode,original);self.assertEqual(len(log.output),3)
        self.assertIn('"http_status": "UNKNOWN"',log.output[0])
