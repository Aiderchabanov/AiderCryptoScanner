import unittest
from unittest.mock import Mock,patch
import requests
import app,basis

class BinanceDiagnosticTests(unittest.TestCase):
    def test_public_http418_logs_status_without_secret_or_response_body(self):
        response=Mock(status_code=418,headers={'Retry-After':'5247'})
        response.json.return_value={'code':-1003,'msg':'SECRET signature=SECRET privateurl=SECRET'}
        response.raise_for_status.side_effect=requests.HTTPError('SECRET',response=response)
        with patch.dict(app.api_blocked_until,{},clear=True),patch.object(app.paper,'load_api_backoff',return_value=0),patch.object(app.paper,'save_api_backoff'),patch.object(app.requests,'get',return_value=response) as get,self.assertLogs(level='WARNING') as logs:
            with self.assertRaises(requests.HTTPError):app.get_json('https://fapi.binance.com/fapi/v1/ticker/bookTicker',params={'signature':'SECRET'})
            with self.assertRaises(RuntimeError):app.get_json('https://fapi.binance.com/fapi/v1/ticker/bookTicker')
        text='\n'.join(logs.output)
        self.assertIn('HTTP=418',text);self.assertIn('Retry-After=5247',text)
        self.assertIn('no HTTP request sent',text);self.assertNotIn('SECRET',text)
        self.assertEqual(get.call_count,1)

    def test_restart_cooldown_makes_no_http_request_and_logs_deadline(self):
        with patch.dict(app.api_blocked_until,{},clear=True),patch.object(app.paper,'load_api_backoff',return_value=app.time.time()+500),patch.object(app.requests,'get') as get,self.assertLogs(level='WARNING') as logs:
            with self.assertRaises(RuntimeError):app.get_json('https://fapi.binance.com/fapi/v1/ticker/bookTicker')
        get.assert_not_called();self.assertIn('HTTP=not_requested','\n'.join(logs.output))
        self.assertIn('until=','\n'.join(logs.output))

    def test_success_only_after_actual_market_response(self):
        api=Mock()
        api.get_json.return_value=[{'symbol':'BTCUSDT','bidPrice':'1','askPrice':'2'}]
        api.gate.return_value=[]
        with self.assertLogs(level='INFO') as logs:
            b,g=basis.futures_markets(api)
        self.assertEqual(len(b),1);self.assertIn('Binance futures market OK: symbols=1','\n'.join(logs.output))
        api.get_json.side_effect=RuntimeError('secret private url')
        with self.assertLogs(level='WARNING') as logs:b,g=basis.futures_markets(api)
        self.assertEqual(b,{})
        self.assertNotIn('secret private url','\n'.join(logs.output))
        self.assertNotIn('market OK','\n'.join(logs.output))
