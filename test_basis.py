import unittest
import time
from decimal import Decimal as D
from unittest.mock import patch, Mock

import app
import basis


class BasisTests(unittest.TestCase):
    def test_funding_direction(self):
        self.assertEqual(basis.funding_expense(D('-0.001'), 'short', D('50'), True), D('0.050'))
        self.assertEqual(basis.funding_expense(D('0.001'), 'long', D('50'), True), D('0.050'))
        self.assertEqual(basis.funding_expense(D('0.001'), 'short', D('50'), True), 0)
        self.assertEqual(basis.funding_expense(D('-0.001'), 'short', D('50'), False), 0)

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
             patch.object(app, 'spot_rules', return_value={'min_qty': D('0.1'), 'min_quote': D('5'), 'step': D('0.01')}), \
             patch.object(app, 'gate', return_value={'funding_rate': '-0.001',
                                                     'funding_next_apply': time.time() + 1800}):
            result = basis.evaluate(app, 'ABCUSDT', 'Binance', 'Gate', '-0.001')
        self.assertEqual(result['quantity'], D('4.8'))
        self.assertGreater(result['projected'], 0)
        self.assertLess(result['projected'], D('1.5'))
        self.assertEqual(result['funding_debit'], D('0.04944'))
        self.assertTrue(result['funding_filtered'])

    def test_funding_before_and_after_settlement_for_short(self):
        metadata = {'step': D('0.1'), 'min_qty': D('0.1'),
                    'min_notional': D('5'), 'multiplier': D('0.1')}
        with patch.object(basis, 'futures_meta', return_value=metadata), \
             patch.object(basis, 'futures_fee', return_value=D('0.001')), \
             patch.object(app, 'fee', return_value=D('0.001')), \
             patch.object(app, 'orderbook', return_value=([['10', '100']], [])), \
             patch.object(basis, 'futures_book', return_value=([], [['10.3', D('100')]])), \
             patch.object(app, 'spot_rules', return_value={'min_qty': D('0.1'), 'min_quote': D('5'), 'step': D('0.01')}), \
             patch.object(app, 'gate') as gate:
            gate.return_value = {'funding_rate': '-0.001', 'funding_next_apply': time.time() + 7200}
            before = basis.evaluate(app, 'ABCUSDT', 'Binance', 'Gate')
            self.assertEqual(before['funding_debit'], 0)
            self.assertTrue(before['funding_filtered'])
            gate.return_value = {'funding_rate': '0.001', 'funding_next_apply': time.time() + 1800}
            positive = basis.evaluate(app, 'ABCUSDT', 'Binance', 'Gate')
            self.assertEqual(positive['funding_debit'], 0)  # Never book projected income.
            self.assertFalse(positive['funding_filtered'])
            gate.return_value = {'funding_rate_next': '0', 'funding_rate': '0.001',
                                 'funding_next_apply': time.time() + 1800}
            zero = basis.evaluate(app, 'ABCUSDT', 'Binance', 'Gate')
            self.assertTrue(zero['funding_filtered'])
            gate.return_value = {}
            with self.assertRaises(ValueError):
                basis.evaluate(app, 'ABCUSDT', 'Binance', 'Gate')

    def test_final_funding_recheck_blocks_episode_and_telegram(self):
        api = Mock()
        api.BINANCE_KEY = api.BINANCE_SECRET = api.GATE_KEY = api.GATE_SECRET = 'configured'
        api.dec = app.dec
        api.tickers.return_value = ({'ABCUSDT': {'askPrice': '10'}}, {})
        candidate = {'symbol': 'ABCUSDT', 'spot': 'Binance', 'future': 'Gate',
                     'pct': D('0.8'), 'funding_filtered': False, 'funding': D('0.001')}
        with patch.object(basis.paper, 'storage_ready', return_value=True), \
             patch.object(basis, 'futures_markets', return_value=({}, {'ABCUSDT': {'highest_bid': '10.3'}})), \
             patch.object(basis, 'evaluate', return_value=candidate), \
             patch.object(basis, 'fresh_funding') as funding, \
             patch.object(basis.paper, 'record') as record:
            for rejected in ((D('-0.001'), time.time() + 1800),
                             (D('0'), time.time() + 1800)):
                funding.return_value = rejected
                self.assertEqual(len(basis.scan(api)), 1)
                record.assert_not_called()
                api.telegram.assert_not_called()
            funding.side_effect = ValueError('funding unavailable')
            basis.scan(api)
            record.assert_not_called()
            api.telegram.assert_not_called()

    def test_funding_snapshot_is_live_and_message_shows_positive_rate(self):
        api = Mock()
        api.dec = app.dec
        api.gate.return_value = {'funding_rate_next': '0.00123456',
                                 'funding_next_apply': time.time() + 1800}
        rate, next_at = basis.fresh_funding(api, 'Gate', 'ABCUSDT')
        self.assertEqual(rate, D('0.00123456'))
        self.assertGreater(next_at, time.time())
        api.gate.assert_called_once_with('/futures/usdt/contracts/ABC_USDT')
        message = basis.format_alert({
            'symbol': 'ABCUSDT', 'pct': D('0.8'), 'projected': D('0.40'),
            'spot_cost': D('50'), 'spot': 'Binance', 'future': 'Gate',
            'quantity': D('5'), 'spot_entry': D('10'), 'future_entry': D('10.3'),
            'spot_exit': D('9.97'), 'future_exit': D('10.03'),
            'spot_fee': D('0.001'), 'future_fee': D('0.001'),
            'funding_debit': D('0'), 'funding': rate, 'next_funding_at': next_at,
            'price_buffer': D('0.1'), 'reserve': D('50')})
        self.assertIn('Funding: +0.1235%', message)
        self.assertIn('Следующий funding:', message)
        self.assertIn('Статус: положительный — условие выполнено.', message)

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
