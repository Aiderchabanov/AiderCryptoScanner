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
        src = [{'chain': 'ETH', 'chainWithdraw': '1', 'withdrawFee': '1',
                'withdrawPercentageFee': '0.02', 'withdrawMin': '2'}]
        dst = [{'network': 'ERC20', 'depositEnable': True}]
        self.assertEqual(app.chain_options(src, dst, 'Bybit', D('100')),
                         [('ETH', D('97.02'), D('2.98'))])
        dst[0]['depositEnable'] = False
        self.assertEqual(app.chain_options(src, dst, 'Bybit', D('100')), [])

    def test_net_uses_both_books_both_trades_and_both_networks(self):
        books = {'Bybit': ([['10', '50'], ['11', '100']], []),
                 'MEXC': ([], [['12', '100']])}
        route = [('ETH', D('94'), D('1'))]
        return_route = [('TRX', D('1126'), D('1'))]
        with patch.object(app, 'orderbook', side_effect=lambda ex, _: books[ex]), \
             patch.object(app, 'fee', return_value=D('0.01')), \
             patch.object(app, 'networks', return_value=[]), \
             patch.object(app, 'chain_options', side_effect=[route, return_route]):
            result = app.estimate('ABCUSDT', 'Bybit', 'MEXC', D('10'), D('12'))
        # First $500 buys 50, next $500 buys 45.4545; fee and transfer are mocked.
        self.assertEqual(result['profit'], D('126'))
        self.assertEqual(result['net'], D('12.6'))

    def test_no_api_keys_means_no_unverified_alerts(self):
        with patch.object(app, 'BYBIT_KEY', ''), patch.object(app, 'tickers') as tickers:
            self.assertEqual(app.scan_once(), [])
            tickers.assert_not_called()


if __name__ == '__main__':
    unittest.main()
