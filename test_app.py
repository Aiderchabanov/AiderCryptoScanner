import unittest
from decimal import Decimal as D
from unittest.mock import patch

import app


class CostTests(unittest.TestCase):
    def test_depth_requires_full_fill(self):
        self.assertIsNone(app.buy_for_usdt([['10', '1']], D('100')))
        self.assertIsNone(app.sell_for_usdt([['12', '1']], D('2')))
        self.assertEqual(app.buy_for_usdt([['10', '2'], ['20', '4']], D('100')), D('6'))

    def test_chain_checks_status_and_fixed_and_percentage_fee(self):
        src = [{'chainId': 'eth', 'isWithdrawEnabled': True, 'withdrawalMinFee': '1',
                'withdrawFeeRate': '0.02', 'withdrawalMinSize': '2'}]
        dst = [{'network': 'ETH', 'depositEnable': True}]
        self.assertEqual(app.chain_options(src, dst, 'KuCoin', D('100')),
                         [('ETH', D('97'), D('3'))])
        dst[0]['depositEnable'] = False
        self.assertEqual(app.chain_options(src, dst, 'KuCoin', D('100')), [])

    def test_chain_rejects_different_token_contract_and_missing_fee(self):
        src = [{'network': 'ETH', 'withdrawEnable': True, 'withdrawFee': '1',
                'withdrawMin': '1', 'contractAddress': '0xabc'}]
        dst = [{'chainId': 'eth', 'isDepositEnabled': True, 'contractAddress': '0xdef'}]
        self.assertEqual(app.chain_options(src, dst, 'Binance', D('100')), [])
        dst[0]['contractAddress'] = '0xabc'
        self.assertEqual(app.chain_options(src, dst, 'Binance', D('100')),
                         [('ETH', D('99'), D('1'))])
        del src[0]['withdrawFee']
        self.assertEqual(app.chain_options(src, dst, 'Binance', D('100')), [])

    def test_net_uses_both_books_both_trades_and_both_networks(self):
        books = {'Binance': ([['10', '50'], ['11', '100']], []),
                 'KuCoin': ([], [['12', '100']])}
        route = [('ETH', D('94'), D('1'))]
        return_route = [('TRX', D('1126'), D('1'))]
        with patch.object(app, 'orderbook', side_effect=lambda ex, _: books[ex]), \
             patch.object(app, 'fee', return_value=D('0.01')), \
             patch.object(app, 'networks', return_value=[]), \
             patch.object(app, 'chain_options', side_effect=[route, return_route]):
            result = app.estimate('ABCUSDT', 'Binance', 'KuCoin', D('10'), D('12'))
        # First $500 buys 50, next $500 buys 45.4545; fee and transfer are mocked.
        self.assertEqual(result['profit'], D('126'))
        self.assertEqual(result['net'], D('12.6'))

    def test_no_api_keys_means_no_unverified_alerts(self):
        with patch.object(app, 'BINANCE_KEY', ''), patch.object(app, 'tickers') as tickers:
            self.assertEqual(app.scan_once(), [])
            tickers.assert_not_called()

    def test_exchange_symbols_and_account_fees(self):
        with patch.object(app, 'binance', side_effect=[[{'symbol': 'BTCUSDT', 'askPrice': '10'}],
                                                    [{'symbol': 'BTCUSDT', 'takerCommission': '0.001'}]]), \
             patch.object(app, 'kucoin', side_effect=[{'ticker': [{'symbol': 'BTC-USDT', 'buy': '11'}]},
                                                   [{'symbol': 'BTC-USDT', 'takerFeeRate': '0.002'}]]):
            b, k = app.tickers()
            self.assertEqual(set(b) & set(k), {'BTCUSDT'})
            self.assertEqual(app.fee('Binance', 'BTCUSDT'), D('0.001'))
            self.assertEqual(app.fee('KuCoin', 'BTCUSDT'), D('0.002'))


if __name__ == '__main__':
    unittest.main()
