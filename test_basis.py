import unittest
from decimal import Decimal as D
from unittest.mock import patch

import app
import basis


class BasisTests(unittest.TestCase):
    def test_gate_contract_book_is_converted_to_coins(self):
        with patch.object(app, 'gate', return_value={'asks': [{'p': '11', 's': '20'}],
                                                     'bids': [{'p': '10', 's': '100'}]}):
            asks, bids = basis.futures_book(app, 'Gate', 'ABCUSDT', D('0.1'))
        self.assertEqual(asks, [['11', D('2.0')]])
        self.assertEqual(bids, [['10', D('10.0')]])

    def test_convergence_requires_full_depth_and_four_fees(self):
        metadata = {'step': D('0.1'), 'min_qty': D('0.1'),
                    'min_notional': D('5'), 'multiplier': D('0.1'), 'funding': D('-0.001')}
        with patch.object(basis, 'futures_meta', return_value=metadata), \
             patch.object(basis, 'futures_fee', return_value=D('0.001')), \
             patch.object(app, 'fee', return_value=D('0.001')), \
             patch.object(app, 'orderbook', return_value=([['10', '100']], [])), \
             patch.object(basis, 'futures_book', return_value=([], [['10.3', D('100')]])), \
             patch.object(basis, 'spot_minimum', return_value=(D('0.1'), D('5'))):
            result = basis.evaluate(app, 'ABCUSDT', 'Binance', 'Gate', '-0.001')
        self.assertEqual(result['quantity'], D('4.8'))
        self.assertGreater(result['projected'], 0)
        self.assertLess(result['projected'], D('1.5'))
        self.assertEqual(result['funding_debit'], D('0.04944'))

    def test_rejects_tiny_liquidity_and_missing_fee(self):
        metadata = {'step': D('0.1'), 'min_qty': D('0.1'),
                    'min_notional': D('5'), 'multiplier': D('0.1'), 'funding': D('0')}
        with patch.object(basis, 'futures_meta', return_value=metadata), \
             patch.object(basis, 'futures_fee', return_value=D('0.001')), \
             patch.object(app, 'fee', return_value=D('0.001')), \
             patch.object(app, 'orderbook', return_value=([['10', '1']], [])), \
             patch.object(basis, 'futures_book', return_value=([], [['10.3', D('100')]])):
            self.assertIsNone(basis.evaluate(app, 'ABCUSDT', 'Binance', 'Gate', '0'))
        with patch.object(app, 'gate', return_value={}), \
             patch.object(app, 'cached', side_effect=lambda _, __, load: load()):
            with self.assertRaises(ValueError):
                basis.futures_fee(app, 'Gate', 'ABCUSDT')

    def test_read_only_and_budget_status(self):
        self.assertEqual(app.app.test_client().get('/').json['basis_leg_usdt'], 50.0)
        self.assertEqual(app.app.test_client().get('/').json['basis_mode'], 'read-only')
        with patch.object(app, 'BINANCE_KEY', ''), patch.object(app, 'tickers') as tickers:
            self.assertEqual(basis.scan(app), [])
            tickers.assert_not_called()


if __name__ == '__main__':
    unittest.main()
