import json
import unittest
from collections import deque
from unittest.mock import Mock, patch
import binance_io as io


class SpotAuditTests(unittest.TestCase):
    def setUp(self):
        self.history = patch.object(io, 'request_history', deque())
        self.history.start()
        self.addCleanup(self.history.stop)

    def test_host_endpoint_counts_and_expiry(self):
        io.request_history.extend([(50, 'api.binance.com', '/api/v3/depth', 5),
                                  (90, 'api.binance.com', '/api/v3/exchangeInfo', 20),
                                  (95, 'fapi.binance.com', '/fapi/v1/commissionRate', 20)])
        counts = io.split_counters(100)
        self.assertEqual(counts['spot_requests_last_60s'], 2)
        self.assertEqual(counts['spot_depth_requests_last_60s'], 1)
        self.assertEqual(counts['futures_commission_requests_last_60s'], 1)
        self.assertEqual(io.endpoint_load('api.binance.com', '/api/v3/depth', 100)['estimated_weight_last_60s_for_host'], 25)
        self.assertEqual(io.split_counters(111)['spot_depth_requests_last_60s'], 0)

    def test_all_counter_categories(self):
        paths = ['/api/v3/depth', '/api/v3/exchangeInfo', '/api/v3/ticker/bookTicker',
                 '/fapi/v1/depth', '/fapi/v1/premiumIndex', '/fapi/v1/commissionRate']
        io.request_history.extend((100, 'api.binance.com' if p.startswith('/api/') else 'fapi.binance.com', p, 1) for p in paths)
        counts = io.split_counters(100)
        for key, value in counts.items():
            self.assertEqual(value, 3 if key in ('spot_requests_last_60s', 'futures_requests_last_60s') else 1)

    def test_sent_record_only_safe_parameters(self):
        with patch.object(io.time, 'monotonic', return_value=100), patch.object(io, 'request_component', return_value='scanner'), patch.object(io.logging, 'info') as log:
            io.request_started('api.binance.com', '/api/v3/depth', {'symbol':'BTCUSDT', 'limit':100, 'signature':'SECRET', 'apiKey':'KEY'})
        encoded = repr(log.call_args_list)
        self.assertNotIn('SECRET', encoded)
        self.assertNotIn('apiKey', encoded)
        self.assertEqual(len(io.request_history), 1)
        self.assertIn('scanner', encoded)

    def test_error_safe_fields_and_no_raw_payload(self):
        response = Mock(status_code=418, headers={'Retry-After':'419', 'X-MBX-USED-WEIGHT':'259'})
        response.json.return_value = {'code':-1003, 'msg':'Too many requests; IP(1.2.3.4) banned until 1791180000000. signature=SECRET', 'secret':'KEY'}
        with patch.object(io.logging, 'warning') as log:
            io.spot_error_observed('api.binance.com', '/api/v3/depth', response, {'symbol':'ROBOUSDT', 'limit':100, 'signature':'SECRET'}, 419)
        result = io.spot_error_diagnostics[-1]
        self.assertEqual(result['binance_code'], -1003)
        self.assertEqual(result['Retry-After'], 419)
        self.assertEqual(result['server_used_weight'], 259)
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertNotIn('1.2.3.4', json.dumps(result))
        self.assertNotIn('KEY', repr(log.call_args))

    def test_missing_and_unrecognized_message(self):
        r = Mock()
        r.json.return_value = {}
        self.assertEqual(io.sanitized_error(r), ('UNKNOWN', 'UNKNOWN'))
        r.json.return_value = {'code':-1000, 'msg':'secret api-key https://private/?signature=KEY'}
        self.assertEqual(io.sanitized_error(r), (-1000, 'REDACTED_UNRECOGNIZED_MESSAGE'))
        r.json.side_effect = ValueError()
        self.assertEqual(io.sanitized_error(r), ('UNKNOWN', 'UNKNOWN'))

    def test_futures_does_not_enter_spot_error_store(self):
        r = Mock(status_code=429)
        with patch.object(io, 'spot_error_diagnostics', deque()) as errors:
            io.spot_error_observed('fapi.binance.com', '/fapi/v1/depth', r, {}, 60)
            self.assertEqual(len(errors), 0)
        r.json.assert_not_called()

    def test_unsafe_symbol_and_limit_redacted(self):
        result = io.request_fields('api.binance.com', '/api/v3/depth', {'symbol':'secret?signature=KEY', 'limit':'https://private'})
        self.assertEqual(result['symbol'], 'UNKNOWN')
        self.assertEqual(result['limit'], 'UNKNOWN')
