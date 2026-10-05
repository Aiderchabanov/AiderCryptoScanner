"""No live HTTP: server weight protection tested with mocked responses."""
import unittest
from contextlib import ExitStack
from collections import deque
from unittest.mock import Mock, patch
import requests
import app
import binance_io as io


class ServerWeightTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        for mapping in (io.weight_headers, io.server_weights, io.weight_limits,
                        io.snapshots, app.api_blocked_until):
            self.stack.enter_context(patch.dict(mapping, {}, clear=True))
        self.stack.enter_context(patch.object(io, 'request_history', deque()))
        self.stack.enter_context(patch.object(io, 'next_request', 0))
        self.stack.enter_context(patch.object(io, 'last_clock', 0))
        self.clock = self.stack.enter_context(patch.object(io.time, 'monotonic', return_value=100))
        self.stack.enter_context(patch.object(io.time, 'sleep'))
        self.stack.enter_context(patch.object(app.paper, 'load_api_backoff', return_value=0))
        self.stack.enter_context(patch.object(app.paper, 'save_api_backoff'))

    def response(self, used, data=None, status=200):
        r = requests.Response(); r.status_code = status
        r.headers['X-MBX-USED-WEIGHT-1M'] = str(used)
        r.json = Mock(return_value={} if data is None else data)
        return r

    def test_success_header_blocks_depth_before_http_without_incrementing_count(self):
        with patch.object(app.requests, 'get', return_value=self.response(2996)) as get:
            app.get_json('https://fapi.binance.com/fapi/v1/ticker/bookTicker')
            with self.assertLogs(level='WARNING') as logs:
                with self.assertRaisesRegex(RuntimeError, 'SERVER_WEIGHT_SAFETY_PAUSE'):
                    app.get_json('https://fapi.binance.com/fapi/v1/depth', params={'limit':50, 'signature':'DO_NOT_LOG'})
        self.assertEqual(get.call_count, 1)
        self.assertEqual(io.rolling_load()['requests'], 1)
        text = '\n'.join(logs.output)
        self.assertIn('request_blocked_before_http=true', text)
        self.assertIn('blocked_endpoint=/fapi/v1/depth', text)
        self.assertNotIn('DO_NOT_LOG', text)
        self.assertEqual(io.weight_headers['fapi.binance.com']['binance_server_used_weight_1m'], 2996)
        self.assertIn('timestamp', io.weight_headers['fapi.binance.com'])

    def test_actual_metadata_defines_limit_and_projected_depth_respects_headroom(self):
        data={'rateLimits':[{'rateLimitType':'REQUEST_WEIGHT','interval':'MINUTE','intervalNum':1,'limit':2400}]}
        with patch.object(app.requests, 'get', return_value=self.response(1919, data)) as get:
            app.get_json('https://fapi.binance.com/fapi/v1/exchangeInfo')
            self.assertEqual(io.weight_limits['fapi.binance.com'], 2400)
            with self.assertRaisesRegex(RuntimeError, 'SERVER_WEIGHT_SAFETY_PAUSE'):
                app.get_json('https://fapi.binance.com/fapi/v1/depth', params={'limit':50})
        get.assert_called_once()

    def test_high_sample_expires_but_low_ticker_does_not_erase_it_early(self):
        io.response_observed('fapi.binance.com','/fapi/v1/depth',self.response(2996))
        self.clock.return_value=120
        io.response_observed('fapi.binance.com','/fapi/v1/ticker/bookTicker',self.response(1))
        with self.assertRaises(RuntimeError): io.server_weight_guard('fapi.binance.com','/fapi/v1/depth',{})
        self.clock.return_value=160
        io.server_weight_guard('fapi.binance.com','/fapi/v1/depth',{})

    def test_futures_pause_does_not_block_spot_or_gate(self):
        io.response_observed('fapi.binance.com','/fapi/v1/depth',self.response(2996))
        with patch.object(app.requests,'get',return_value=self.response(1)) as get:
            app.get_json('https://api.binance.com/api/v3/ticker/bookTicker')
            app.get_json('https://api.gateio.ws/api/v4/spot/tickers')
        self.assertEqual(get.call_count,2)

    def test_invalid_headers_or_nonminute_metadata_cannot_define_official_limit(self):
        r=self.response('SECRET');r.headers['X-MBX-USED-WEIGHT-5M']='9000'
        io.response_observed('fapi.binance.com','/fapi/v1/depth',r)
        io.metadata_observed('fapi.binance.com','/fapi/v1/exchangeInfo',{'rateLimits':[{'rateLimitType':'ORDERS','interval':'MINUTE','intervalNum':1,'limit':10}]})
        self.assertNotIn('fapi.binance.com',io.weight_limits)
        io.server_weight_guard('fapi.binance.com','/fapi/v1/depth',{})

    def test_existing_retry_after_blocks_before_server_guard_and_http(self):
        bad=self.response(2996,status=429);bad.headers['Retry-After']='15'
        with patch.object(app.requests,'get',return_value=bad) as get:
            with self.assertRaises(requests.HTTPError):app.get_json('https://fapi.binance.com/fapi/v1/depth')
            self.clock.return_value=114
            with self.assertRaisesRegex(RuntimeError,'Exchange API temporarily unavailable'):
                app.get_json('https://fapi.binance.com/fapi/v1/depth')
        get.assert_called_once()
        self.assertEqual(app.api_blocked_until['fapi.binance.com'],115)


if __name__ == '__main__':
    unittest.main()
