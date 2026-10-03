import hashlib,hmac,unittest
from urllib.parse import urlencode,urlparse
from unittest.mock import MagicMock,patch
import requests,app,basis

class CommissionDiagnosticTests(unittest.TestCase):
    def test_signed_query_matches_requests_wire_encoding(self):
        with patch.object(app,'BINANCE_KEY','synthetic-key'),patch.object(app,'BINANCE_SECRET','synthetic-secret'),patch.object(app.time,'time',return_value=1700000000):
            params=app.binance_signed_params({'symbol':'BTCUSDT'})
        request=requests.Request('GET',basis.FUTURES_BINANCE+'/fapi/v1/commissionRate',params=params,headers={'X-MBX-APIKEY':'synthetic-key'}).prepare()
        query=urlparse(request.url).query
        unsigned,signature=query.rsplit('&signature=',1)
        self.assertEqual(signature,hmac.new(b'synthetic-secret',unsigned.encode(),hashlib.sha256).hexdigest())
        self.assertEqual(params['timestamp'],1700000000000)
        self.assertEqual(params['recvWindow'],5000)
        self.assertEqual(params['symbol'],'BTCUSDT')
        self.assertEqual(request.headers['X-MBX-APIKEY'],'synthetic-key')

    def test_error_safe_and_existing_backoff_preserved(self):
        response=MagicMock(status_code=401,headers={'Retry-After':'7200'})
        response.json.return_value={'code':-2015,'msg':'Invalid API-key, IP, or permissions for action.'}
        response.raise_for_status.side_effect=requests.HTTPError('not logged')
        with patch.object(app,'api_blocked_until',{}),patch.object(app.paper,'load_api_backoff',return_value=0),patch.object(app.requests,'get',return_value=response),self.assertLogs(level='WARNING') as captured:
            with self.assertRaises(requests.HTTPError):app.get_json(basis.FUTURES_BINANCE+'/fapi/v1/commissionRate',params={'signature':'synthetic-secret'})
            self.assertGreater(app.api_blocked_until[('fapi.binance.com','/fapi/v1/commissionRate')],app.time.monotonic()+7198)
        logs=' '.join(captured.output)
        self.assertIn('error code=-2015',logs);self.assertIn('Retry-After=7200',logs)
        self.assertNotIn('synthetic-secret',logs)

    def test_unrecognized_body_and_header_never_leak(self):
        response=MagicMock(status_code=401,headers={'Retry-After':'secret-value'})
        response.json.return_value={'code':-2015,'msg':'https://private/?signature=secret-value'}
        with self.assertLogs(level='WARNING') as captured:app.log_binance_commission_response(response)
        self.assertNotIn('secret-value',' '.join(captured.output))
        self.assertNotIn('https://',' '.join(captured.output))
