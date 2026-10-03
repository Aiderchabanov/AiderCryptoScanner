import hashlib
import hmac
import json
import tempfile
import time
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import Mock, patch
import app
import basis
import bingx
import exchanges
import paper
from test_paper import example


def client():
    c = bingx.Client()
    c.enabled = True
    c.key, c.secret = 'test-key', 'test-secret'
    return c


def response(data, status=200, code=0, headers=None):
    r = Mock(status_code=status, headers=headers or {})
    r.json.return_value = {'code': code, 'data': data}
    return r


class BingXTests(unittest.TestCase):
    def test_get_only_signing_and_no_secret_in_errors(self):
        c = client()
        with patch.object(bingx.time, 'time', return_value=1234567), \
             patch.object(bingx.requests, 'get', return_value=response({})) as get:
            c.request('spot_fee', {'symbol': 'BTC-USDT'})
        args = get.call_args
        self.assertTrue(args.args[0].startswith(bingx.BASE_URL))
        canonical = 'recvWindow=5000&symbol=BTC-USDT&timestamp=1234567000'
        expected = hmac.new(b'test-secret', canonical.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(args.kwargs['params']['signature'], expected)
        self.assertEqual(args.kwargs['headers']['X-BX-APIKEY'], 'test-key')
        self.assertFalse(args.kwargs['allow_redirects'])
        for endpoint in ('order', 'withdraw', 'transfer', '/openApi/swap/v2/trade/order'):
            with self.assertRaises(bingx.BingXUnavailable):
                c.request(endpoint)
        with patch.object(bingx.requests, 'get', side_effect=RuntimeError('not called')):
            with self.assertRaises(bingx.BingXUnavailable):
                c.request('spot_fee', {'symbol': 'BTC-USDT&secret=test-secret'})

    def test_429_and_api_limit_cooldown_honour_retry_after(self):
        for status, code in ((429, 0), (200, 100410), (403, 0)):
            c = client()
            with patch.object(bingx.requests, 'get', return_value=response({}, status, code, {'Retry-After': '7200'})) as get:
                with self.assertRaises(bingx.BingXUnavailable) as error:
                    c.request('spot_symbols')
                self.assertNotIn('test-secret', str(error.exception))
                with self.assertRaisesRegex(bingx.BingXUnavailable, 'cooldown'):
                    c.request('contracts')
                get.assert_called_once()
                if status != 200:
                    self.assertGreater(c._blocked_until, time.monotonic() + 7100)

    def test_global_pacing_and_unknown_fees_fail_closed(self):
        c = client()
        with patch.object(bingx.requests, 'get', return_value=response({})), \
             patch.object(bingx.time, 'sleep') as sleep:
            c.request('spot_symbols')
            c.request('contracts')
            self.assertGreater(sleep.call_args.args[0], 1)
        for data, futures in (({}, False), ({'commission': {}}, True),
                               ({'takerCommissionRate': '-0.1'}, False),
                               ({'takerCommissionRate': 'NaN'}, False)):
            with patch.object(c, 'request', return_value=data):
                with self.assertRaises(bingx.BingXUnavailable):
                    c.fee('BTCUSDT', futures)
        with patch.object(c, 'request', return_value={'commission': {'takerCommissionRate': '0.0005'}}):
            self.assertEqual(c.fee('BTCUSDT', True), D('0.0005'))

    def test_funding_is_uncached_and_future_settlement_mandatory(self):
        c = client()
        now = time.time()
        row = {'symbol': 'BTC-USDT', 'lastFundingRate': '0.0001',
               'nextFundingTime': (now+18000)*1000, 'time': now*1000}
        with patch.object(c, 'request', return_value=row) as request:
            for _ in range(2):
                rate, next_at = c.fresh_funding('BTCUSDT')
                self.assertEqual(rate, D('0.0001'))
                self.assertGreater(next_at, now)
            self.assertEqual(request.call_count, 2)
            for key in ('lastFundingRate', 'nextFundingTime'):
                missing = dict(row); missing.pop(key)
                request.return_value = missing
                with self.assertRaises(bingx.BingXUnavailable):
                    c.fresh_funding('BTCUSDT')
            request.return_value = {k: v for k, v in row.items() if k != 'time'}
            self.assertEqual(c.fresh_funding('BTCUSDT')[0], D('0.0001'))

    def test_books_validate_timestamp_and_depth(self):
        c = client()
        row = {'T': time.time()*1000, 'asks': [['11', '5']], 'bids': [['10', '4']]}
        with patch.object(c, 'request', return_value=row) as request:
            asks, bids = c.orderbook('BTCUSDT', True)
            self.assertEqual(bids, [[D('10'), D('4')]])
            request.return_value = dict(row, T=(time.time()-60)*1000)
            with self.assertRaises(bingx.BingXUnavailable):
                c.orderbook('BTCUSDT', True)
            request.return_value = dict(row, bids=[['12', '4']])
            with self.assertRaises(bingx.BingXUnavailable):
                c.orderbook('BTCUSDT', True)

    def test_reversed_live_spot_asks_are_normalized(self):
        c = client()
        row = {'ts': time.time()*1000, 'asks': [['12', '5'], ['11', '2']], 'bids': [['10', '4']]}
        with patch.object(c, 'request', return_value=row):
            asks, _ = c.orderbook('BTCUSDT')
            self.assertEqual(asks[0][0], D('11'))
            self.assertEqual(app.buy_for_usdt(asks, D('22')), D('2'))

    def test_bad_ticker_is_excluded_without_using_last_price(self):
        c = client()
        with patch.object(c, 'spot_symbols', return_value={'BTCUSDT': {}, 'ABCUSDT': {}}), \
             patch.object(c, 'request', return_value=[
                 {'symbol': 'BTC-USDT', 'askPrice': '11', 'bidPrice': '10'},
                 {'symbol': 'ABC-USDT', 'askPrice': 0, 'lastPrice': '999', 'bidPrice': '9'}]):
            self.assertEqual(set(c.tickers()), {'BTCUSDT'})

    def test_live_futures_book_response_shape(self):
        c = client()
        with patch.object(c, 'request', return_value={'book_ticker': {
                'symbol': 'BTC-USDT', 'bid_price': 10, 'ask_price': 11}}):
            self.assertEqual(c.book_ticker('BTCUSDT', True)['bidPrice'], D('10'))

    def test_limits_and_network_unknowns_are_never_zero(self):
        c = client()
        with patch.object(c, 'request', return_value=[{'coin': 'USDT', 'networkList': [
            {'network': 'TRC20', 'depositEnable': True, 'withdrawEnable': False}]}]):
            network = c.networks('USDT')[0]
            self.assertIsNone(network['withdrawFee'])
            self.assertIsNone(network['depositFee'])
            self.assertIs(network['withdrawEnable'], False)
        meta = {'status': 1, 'symbol': 'BTC-USDT', 'currency': 'USDT',
                'quantityPrecision': 3, 'tradeMinQuantity': '0.001', 'tradeMinUSDT': '5',
                'apiStateOpen': 'true', 'apiStateClose': 'true'}
        with patch.object(c, 'request', return_value=[meta]):
            self.assertEqual(c.futures_meta('BTCUSDT')['step'], D('0.001'))
            meta.pop('tradeMinUSDT')
            with self.assertRaises(bingx.BingXUnavailable):
                c.futures_meta('BTCUSDT')

    def test_all_six_directions_and_existing_routes_survive(self):
        api = exchanges.ScannerAPI(app, client())
        spots = {x: {'ABCUSDT': {'askPrice': '10', 'lowest_ask': '10'}} for x in ('Binance', 'Gate', 'BingX')}
        futures = {x: {'ABCUSDT': {'bidPrice': '10.5', 'highest_bid': '10.5'}} for x in spots}
        with patch.object(api, 'market_maps', return_value=(spots, futures)), \
             patch.object(api, 'credentials_ready', return_value=True), \
             patch.object(paper, 'storage_ready', return_value=True), \
             patch.object(basis, 'evaluate', return_value=None) as evaluate:
            basis.scan(api)
        pairs = {c.args[2:4] for c in evaluate.call_args_list}
        self.assertEqual(pairs, {(s,f) for s in spots for f in futures if s != f})
        with patch.object(app, 'BINANCE_KEY', 'key'), patch.object(app, 'BINANCE_SECRET', 'secret'), \
             patch.object(app, 'GATE_KEY', 'key'), patch.object(app, 'GATE_SECRET', 'secret'), \
             patch.object(app, 'binance', return_value=[{'symbol': 'ABCUSDT'}]), \
             patch.object(app, 'gate', return_value=[{'currency_pair': 'ABC_USDT'}]), \
             patch.object(basis, 'futures_markets', return_value=({'ABCUSDT': {}}, {'ABCUSDT': {}})), \
             patch.object(api.bingx, 'tickers', side_effect=bingx.BingXUnavailable('offline')):
            live_spots, live_futures = api.market_maps()
        self.assertIn('Binance', live_spots); self.assertIn('Gate', live_spots)
        self.assertIn('Binance', live_futures); self.assertIn('Gate', live_futures)

    def test_virtual_gate_bingx_entry_and_half_deposit_limit(self):
        api = exchanges.ScannerAPI(app, client())
        meta = {'step': D('0.01'), 'min_qty': D('0.01'), 'min_notional': D('5'), 'multiplier': D('1')}
        with patch.object(api.bingx, 'futures_meta', return_value=meta), \
             patch.object(api.bingx, 'fee', return_value=D('0.0005')), \
             patch.object(api.bingx, 'fresh_funding', return_value=(D('0.0001'), time.time()+18000)), \
             patch.object(api.bingx, 'orderbook', return_value=([['10.6', D('100')]], [['10.5', D('100')]])), \
             patch.object(app, 'orderbook', return_value=([['10', '100']], [['9.99','100']])), \
             patch.object(app, 'fee', return_value=D('0.001')), \
             patch.object(app, 'spot_rules', return_value={'min_qty': D('0.01'), 'min_quote': D('5'), 'step': D('0.01')}):
            item = basis.evaluate(api, 'ABCUSDT', 'Gate', 'BingX')
        self.assertTrue(basis.qualifies(item))
        self.assertLessEqual(item['spot_cost'], D('50'))
        self.assertLessEqual(item['future_notional'], D('50'))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'isolated-test.sqlite'
            ident = paper.record(item, path)
            self.assertIsNotNone(ident)
            other = dict(item, symbol='XYZUSDT')
            self.assertIsNotNone(paper.record(other, path))  # Aggregate capital still below $250.
            third = dict(item, symbol='THIRDUSDT')
            self.assertIsNone(paper.record(third, path))  # Spot capital plus 1x Futures margin exceeds $250.
            with paper.session(path) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM checkpoints').fetchone()[0], 18)
                self.assertNotIn('test-secret', db.execute('SELECT cost_snapshot_json FROM episodes').fetchone()[0])
        text = basis.format_alert(item)
        self.assertIn('PAPER / VIRTUAL ONLY', text)
        self.assertIn('BingX', text)


if __name__ == '__main__':
    unittest.main()
