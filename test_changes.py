import tempfile
import runpy
import time
import unittest
from pathlib import Path
from decimal import Decimal as D
from unittest.mock import Mock, patch
import app
import basis
import paper
from test_paper import example, sample


class ChangesTests(unittest.TestCase):
    def test_spot_spot_disabled_even_if_called(self):
        with patch.object(app, 'tickers') as tickers, patch.object(app, 'telegram') as telegram:
            self.assertEqual(app.scan_once(), [])
            tickers.assert_not_called()
            telegram.assert_not_called()
        self.assertFalse(app.app.test_client().get('/').json['spot_spot_enabled'])

    def test_startup_starts_only_futures_and_observations(self):
        with patch('threading.Thread') as thread, \
             patch.object(paper, 'storage_ready', return_value=True), \
             patch('flask.Flask.run'), \
             patch('requests.get', side_effect=RuntimeError('offline')):
            runpy.run_path(str(Path(app.__file__)), run_name='__main__')
        targets = [c.kwargs['target'].__name__ for c in thread.call_args_list]
        self.assertEqual(targets, ['loop', 'loop', 'loop', 'basis_loop', 'paper_loop'])
        self.assertEqual([c.kwargs.get('name') for c in thread.call_args_list[:3]], ['risk-Binance','risk-Gate','risk-BingX'])
        self.assertTrue(all(c.kwargs['target'] is not app.loop for c in thread.call_args_list))

    def test_negative_executable_depth_and_funding_boundaries(self):
        now = 1000000000
        meta = {'step': D('0.01'), 'min_qty': D('0.01'), 'min_notional': D('5'), 'multiplier': D('1')}
        with patch.object(basis.time, 'time', return_value=now), \
             patch.object(basis, 'futures_meta', return_value=meta), \
             patch.object(basis, 'futures_fee', return_value=D('0.001')), \
             patch.object(app, 'fee', return_value=D('0.001')), \
             patch.object(app, 'orderbook', return_value=([['10', '100']], [])), \
             patch.object(app, 'spot_rules', return_value={'min_qty': D('0.01'), 'min_quote': D('5'), 'step': D('0.01')}), \
             patch.object(basis, 'fresh_funding') as funding, \
             patch.object(basis, 'futures_book') as book:
            for bid, rate, remaining, allowed in (
                ('9.8', '0.001', 14400, True), ('9.9', '0.001', 14400, True),
                ('9.799', '0.001', 14400, False), ('9.9', '0.001', 14399, False),
                ('9.9', '0', 18000, False), ('9.9', '-0.001', 18000, False),
                ('10', '0.001', 18000, False)):
                with self.subTest(bid=bid, rate=rate, remaining=remaining):
                    funding.return_value = (D(rate), now + remaining)
                    book.return_value = ([], [[bid, D('100')]])
                    item = basis.evaluate(app, 'ABCUSDT', 'Binance', 'Gate')
                    self.assertEqual(item is not None, allowed)
                    if item:
                        self.assertTrue(basis.qualifies(item, now))
                        self.assertEqual(item['category'], 'NEGATIVE')
                        self.assertLess(item['pct'], 0)  # No imaginary profit threshold.
            funding.return_value = (D('0.001'), now + 18000)
            book.return_value = ([], [['9.9', D('0.01')], ['9.7', D('100')]])
            self.assertIsNone(basis.evaluate(app, 'ABCUSDT', 'Binance', 'Gate'))

    def test_negative_rechecked_before_record_and_telegram(self):
        api = Mock()
        api.BINANCE_KEY = api.BINANCE_SECRET = api.GATE_KEY = api.GATE_SECRET = 'configured'
        api.dec = app.dec
        api.tickers.return_value = ({'ABCUSDT': {'askPrice': '10'}}, {'ABCUSDT': {'lowest_ask': '10'}})
        now = time.time()
        item = example()
        item.update(symbol='ABCUSDT', spot='Binance', category='NEGATIVE',
                    raw_spread_pct=D('-1'), executable_spread_pct=D('-1'),
                    funding_filtered=False, next_funding_at=now+18000)
        with patch.object(paper, 'storage_ready', return_value=True), \
             patch.object(basis, 'futures_markets', return_value=({'ABCUSDT': {'bidPrice': '9.9'}}, {'ABCUSDT': {'highest_bid': '9.9'}})), \
             patch.object(basis, 'evaluate', return_value=item) as evaluate, \
             patch.object(basis, 'fresh_funding', return_value=(D('0.001'), now+14399)), \
             patch.object(paper, 'record') as record:
            basis.scan(api)
            self.assertEqual(evaluate.call_count, 2)
            self.assertEqual({c.args[2:4] for c in evaluate.call_args_list}, {('Binance', 'Gate'), ('Gate', 'Binance')})
            record.assert_not_called()
            api.telegram.assert_not_called()

    def test_negative_stats_and_zero_crossing_are_observed_not_assumed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'test.sqlite'
            now = 1000000000
            item = example()
            item.update(future_bid=D('9.9'), future_entry=D('9.9'), next_funding_at=now+18000)
            ident = paper.record(item, path, now)
            self.assertIsNotNone(ident)
            with patch.object(paper, 'executable_sample', return_value=sample('-0.5')):
                paper.poll(app, path, now+60)
            with paper.session(path) as db:
                self.assertIsNone(db.execute('SELECT first_close_min FROM episodes').fetchone()[0])
            with patch.object(paper, 'executable_sample', return_value=sample('0')):
                paper.poll(app, path, now+300)
            with patch.object(paper, 'executable_sample', return_value=sample('0.2')):
                paper.poll(app, path, now+900)
            with paper.session(path) as db:
                stats = paper.negative_statistics(db)
                self.assertEqual(stats['count'], 1)
                self.assertEqual(stats['reached_zero'], 1)
                self.assertEqual(stats['became_positive'], 1)
                self.assertEqual(stats['mean_entry'], D('-1'))
                self.assertIsNone(stats['mean_zero_min'])
                self.assertEqual(db.execute('SELECT first_close_min FROM episodes').fetchone()[0], 5)
                # Preserve but exclude a legacy Spot/Spot row from primary stats.
                db.execute("UPDATE episodes SET direction='spot_buy/spot_sell' WHERE id=?", (ident,))
                self.assertEqual(paper.statistics_for(db)['observations'], 0)
                self.assertEqual(paper.negative_statistics(db)['count'], 0)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM episodes').fetchone()[0], 1)

    def test_negative_persistence_rejects_invalid_range_or_short_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'test.sqlite'
            for bid, seconds in (('9.79', 18000), ('9.9', 14399)):
                item = example()
                item.update(future_bid=D(bid), future_entry=D(bid), next_funding_at=1000000000+seconds)
                self.assertIsNone(paper.record(item, path, 1000000000))


if __name__ == '__main__':
    unittest.main()
