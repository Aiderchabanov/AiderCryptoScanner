import unittest
from decimal import Decimal as D
from unittest.mock import patch

import app

RULES = {'min_qty': D('0.01'), 'min_quote': D('1'), 'max_qty': None,
         'max_quote': None, 'market_max_qty': None, 'market_max_quote': None,
         'step': D('0.00000001')}


class CostTests(unittest.TestCase):
    def test_depth_requires_full_fill(self):
        self.assertIsNone(app.buy_for_usdt([['10', '1']], D('100')))
        self.assertIsNone(app.sell_for_usdt([['12', '1']], D('2')))
        self.assertEqual(app.buy_for_usdt([['10', '2'], ['20', '4']], D('100')), D('6'))

    def test_chain_checks_status_and_fixed_and_percentage_fee(self):
        src = [{'network': 'ETH', 'withdrawEnable': True, 'withdrawFee': '1',
                'withdrawRate': '2%', 'withdrawMin': '2'}]
        dst = [{'network': 'ETH', 'depositEnable': True}]
        self.assertEqual(app.chain_options(src, dst, 'Gate', D('100')),
                         [('ETH', D('97'), D('3'))])
        dst[0]['depositEnable'] = False
        self.assertEqual(app.chain_options(src, dst, 'Gate', D('100')), [])

    def test_chain_rejects_different_token_contract_and_missing_fee(self):
        src = [{'network': 'ETH', 'withdrawEnable': True, 'withdrawFee': '1',
                'withdrawMin': '1', 'contractAddress': '0xabc'}]
        dst = [{'network': 'ETH', 'depositEnable': True, 'depositFee': '0', 'contractAddress': '0xdef'}]
        self.assertEqual(app.chain_options(src, dst, 'Binance', D('100')), [])
        dst[0]['contractAddress'] = '0xabc'
        self.assertEqual(app.chain_options(src, dst, 'Binance', D('100')),
                         [('ETH', D('99'), D('1'))])
        del src[0]['withdrawFee']
        self.assertEqual(app.chain_options(src, dst, 'Binance', D('100')), [])

    def test_net_uses_both_books_both_trades_and_both_networks(self):
        books = {'Binance': ([['10', '50'], ['11', '100']], []),
                 'Gate': ([], [['12', '100']])}
        route = [('ETH', D('4.9'), D('0.05'))]
        return_route = [('TRX', D('57.212'), D('1'))]
        with patch.object(app, 'orderbook', side_effect=lambda ex, _: books[ex]), \
             patch.object(app, 'spot_rules', return_value=RULES), \
             patch.object(app, 'fee', return_value=D('0.01')), \
             patch.object(app, 'networks', return_value=[]), \
             patch.object(app, 'chain_options', side_effect=[route, return_route]):
            result = app.estimate('ABCUSDT', 'Binance', 'Gate', D('10'), D('12'))
        # $50 buys 5 units, then fees, both transfers and $0.10 buffer.
        self.assertEqual(result['profit'], D('7.112'))
        self.assertEqual(result['net'], D('14.224'))
        self.assertEqual(result['price_buffer'], D('0.10'))
        self.assertEqual(result['buy_price'], D('10'))
        self.assertEqual(result['sell_price'], D('12'))

    def test_depth_slippage_and_extra_buffer_are_not_double_counted(self):
        books = {'Binance': ([['10', '2'], ['11', '10']], []),
                 'Gate': ([], [['12', '2'], ['11', '10']])}
        with patch.object(app, 'orderbook', side_effect=lambda ex, _: books[ex]), \
             patch.object(app, 'spot_rules', return_value=RULES), \
             patch.object(app, 'fee', return_value=D('0')), \
             patch.object(app, 'networks', return_value=[]), \
             patch.object(app, 'chain_options', side_effect=lambda _s, _d, ex, amount:
                          [('ETH', amount, D('0'))] if ex == 'Binance'
                          else [('TRX', amount, D('0'))]):
            result = app.estimate('ABCUSDT', 'Binance', 'Gate', D('10'), D('12'))
        self.assertGreater(result['buy_slippage'], 0)
        self.assertGreater(result['sell_slippage'], 0)
        self.assertAlmostEqual(result['profit'], result['sell_price'] * result['sell_qty']
                               - D('50') - D('0.10'), places=10)

    def test_http_418_respects_retry_after_and_stops_repeat_calls(self):
        response = unittest.mock.Mock(status_code=418, headers={'Retry-After': '7200'})
        response.raise_for_status.side_effect = __import__('requests').HTTPError(response=response)
        with patch.object(app.requests, 'get', return_value=response) as get, \
             patch.object(app.time, 'monotonic', return_value=100):
            with self.assertRaises(__import__('requests').HTTPError):
                app.get_json('https://fapi.binance.com/fapi/v1/commissionRate')
            self.assertEqual(app.api_blocked_until['fapi.binance.com'], 7300)
            with self.assertRaises(RuntimeError):
                app.get_json('https://fapi.binance.com/fapi/v1/commissionRate')
            get.assert_called_once()
        app.api_blocked_until.clear()

    def test_no_api_keys_means_no_unverified_alerts(self):
        with patch.object(app, 'BINANCE_KEY', ''), patch.object(app, 'tickers') as tickers:
            self.assertEqual(app.scan_once(), [])
            tickers.assert_not_called()

    def test_market_order_minimums_and_precision(self):
        rules = dict(RULES, min_qty=D('0.1'), min_quote=D('10'), step=D('0.01'))
        self.assertFalse(app.order_size_ok(rules, D('0.09'), D('20')))
        self.assertFalse(app.order_size_ok(rules, D('0.2'), D('9')))
        self.assertTrue(app.order_size_ok(rules, D('0.2'), D('10')))
        self.assertEqual(app.sale_quantity(D('0.219'), rules), D('0.21'))

    def test_unavailable_pair_rules_do_not_send_alert(self):
        books = {'Binance': ([['10', '5']], []), 'Gate': ([], [['12', '5']])}
        with patch.object(app, 'orderbook', side_effect=lambda ex, _: books[ex]), \
             patch.object(app, 'spot_rules', side_effect=RuntimeError('API unavailable')):
            with self.assertRaises(RuntimeError):
                app.estimate('ABCUSDT', 'Binance', 'Gate', D('10'), D('12'))

    def test_sale_below_minimum_blocks_profitable_book(self):
        books = {'Binance': ([['10', '5']], []), 'Gate': ([], [['12', '5']])}
        buy_rules = dict(RULES)
        sell_rules = dict(RULES, min_qty=D('6'))
        with patch.object(app, 'orderbook', side_effect=lambda ex, _: books[ex]), \
             patch.object(app, 'spot_rules', side_effect=[buy_rules, sell_rules]), \
             patch.object(app, 'fee', return_value=D('0')), \
             patch.object(app, 'networks', return_value=[]), \
             patch.object(app, 'chain_options', return_value=[('ETH', D('5'), D('0'))]):
            self.assertIsNone(app.estimate('ABCUSDT', 'Binance', 'Gate', D('10'), D('12')))

    def test_suspended_network_blocks_both_coin_and_usdt_route(self):
        src = [{'network': 'ETH', 'withdrawEnable': False, 'withdrawFee': '0',
                'withdrawMin': '0'}]
        dst = [{'network': 'ETH', 'depositEnable': True}]
        self.assertEqual(app.chain_options(src, dst, 'Binance', D('5')), [])
        src[0]['withdrawEnable'] = True
        dst[0]['depositEnable'] = False
        self.assertEqual(app.chain_options(src, dst, 'Binance', D('5')), [])
        dst[0]['depositEnable'] = True
        dst[0]['network'] = 'TRX'
        self.assertEqual(app.chain_options(src, dst, 'Binance', D('5')), [])

    def test_spot_rules_from_both_exchange_apis(self):
        binance_info = {'symbols': [{'symbol': 'ABCUSDT', 'status': 'TRADING',
                                     'orderTypes': ['MARKET'], 'filters': [
            {'filterType': 'LOT_SIZE', 'stepSize': '0.01', 'minQty': '0.1', 'maxQty': '100'},
            {'filterType': 'MARKET_LOT_SIZE', 'stepSize': '0', 'minQty': '0', 'maxQty': '100'},
            {'filterType': 'MIN_NOTIONAL', 'minNotional': '10'}]}]}
        gate_info = {'id': 'ABC_USDT', 'trade_status': 'tradable', 'min_base_amount': '0.2',
                     'min_quote_amount': '5', 'amount_precision': 2}
        with patch.object(app, 'binance', return_value=binance_info), \
             patch.object(app, 'gate', return_value=gate_info):
            b = app.spot_rules('Binance', 'ABCUSDT', 'buy')
            g = app.spot_rules('Gate', 'ABCUSDT', 'sell')
        self.assertEqual((b['min_qty'], b['min_quote'], b['step']),
                         (D('0.1'), D('10'), D('0.01')))
        self.assertFalse(app.order_size_ok(g, D('0.19'), D('50')))
        self.assertEqual(g['step'], D('0.01'))

    def test_exchange_symbols_and_account_fees(self):
        with patch.object(app, 'binance', side_effect=[[{'symbol': 'BTCUSDT', 'askPrice': '10'}],
                                                    [{'symbol': 'BTCUSDT', 'takerCommission': '0.001'}]]), \
             patch.object(app, 'gate', side_effect=[[{'currency_pair': 'BTC_USDT', 'highest_bid': '11'}],
                                                   {'taker_fee': '0.002'}]):
            b, g = app.tickers()
            self.assertEqual(set(b) & set(g), {'BTCUSDT'})
            self.assertEqual(app.fee('Binance', 'BTCUSDT'), D('0.001'))
            self.assertEqual(app.fee('Gate', 'BTCUSDT'), D('0.002'))

    def test_gate_networks_use_chain_specific_costs(self):
        chains = [{'chain': 'ETH', 'is_disabled': 0, 'is_deposit_disabled': 0,
                   'is_withdraw_disabled': 0, 'contract_address': '0xabc'},
                  {'chain': 'TRX', 'is_disabled': 0, 'is_deposit_disabled': 0,
                   'is_withdraw_disabled': 1, 'contract_address': ''}]
        status = [{'currency': 'USDT', 'deposit': '0', 'withdraw_amount_mini': '5',
                   'withdraw_eachtime_limit': '1000', 'withdraw_fix_on_chains': {'ETH': '2'},
                   'withdraw_percent_on_chains': {'ETH': '1%'}, 'withdraw_percent': '0%'}]
        with patch.object(app, 'gate', side_effect=[chains, status]):
            rows = app.networks('Gate', 'USDT')
        self.assertEqual(rows[0]['withdrawFee'], '2')
        self.assertEqual(rows[0]['withdrawRate'], '1%')
        self.assertFalse(rows[1]['withdrawEnable'])
        self.assertIsNone(rows[1]['withdrawFee'])


if __name__ == '__main__':
    unittest.main()
