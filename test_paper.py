import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch

import app
import paper


def example():
    return {'symbol': 'HBARUSDT', 'spot': 'Binance', 'future': 'Gate',
            'quantity': D('5'), 'spot_cost': D('50'), 'future_notional': D('50.9'),
            'spot_ask': D('10'), 'spot_entry': D('10'),
            'future_bid': D('10.2'), 'future_entry': D('10.18'),
            'pct': D('0.74'), 'funding': D('0.0001'),
            'next_funding_at': 2000000000, 'funding_debit': D('0'),
            'spot_fee': D('0.001'), 'future_fee': D('0.001'),
            'price_buffer': D('0.10'), 'multiplier': D('1')}


def sample(spread):
    ask = D('10')
    bid = ask * (1 + D(spread) / 100)
    return {'spot_ask': ask, 'spot_vwap': ask, 'futures_bid': bid,
            'futures_vwap': bid, 'raw_spread_pct': D(spread),
            'executable_spread_pct': D(spread)}


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'history.sqlite3'
        self.start = 1000000000

    def test_five_real_checkpoints_episode_and_restart(self):
        item = example()
        episode = paper.record(item, self.path, self.start)
        self.assertIsNotNone(episode)
        self.assertIsNone(paper.record(item, self.path, self.start + 30))
        for minute, spread in zip(paper.HORIZONS, ('1.2', '0.8', '0.4', '0.05', '0.02')):
            with patch.object(paper, 'executable_sample', return_value=sample(spread)):
                paper.poll(app, self.path, self.start + minute * 60)
        # Reopen database as a new process would after restart.
        with paper.session(self.path) as db:
            row = db.execute('SELECT * FROM episodes WHERE id=?', (episode,)).fetchone()
            checkpoints = db.execute('SELECT * FROM checkpoints ORDER BY horizon_min').fetchall()
            stats = paper.statistics_for(db, 'HBARUSDT')
        self.assertEqual([x['status'] for x in checkpoints], ['observed'] * 5)
        self.assertEqual([x['executable_spread_pct'] for x in checkpoints],
                         ['1.2', '0.8', '0.4', '0.05', '0.02'])
        self.assertEqual(row['first_close_min'], 30)
        self.assertEqual(row['min_spread_pct'], '0.02')
        self.assertEqual(stats['closed_by_pct'], {'1': 0.0, '5': 0.0,
                                                '15': 0.0, '30': 100.0, '60': 100.0})
        self.assertEqual(stats['median_close_min'], 30)
        self.assertEqual(stats['mean_close_min'], 30)
        self.assertIn('недостаточно истории', paper.history_line(self.path, 'HBARUSDT'))
        self.assertIsNotNone(paper.record(item, self.path, self.start + 3700))

    def test_missed_window_is_not_reconstructed_from_current_book(self):
        paper.record(example(), self.path, self.start)
        with patch.object(paper, 'executable_sample', side_effect=AssertionError('no backfill')):
            paper.poll(app, self.path, self.start + 3600 + 61)
        with paper.session(self.path) as db:
            statuses = db.execute('SELECT status FROM checkpoints ORDER BY horizon_min').fetchall()
            stats = paper.statistics_for(db)
            episode = db.execute('SELECT status FROM episodes').fetchone()
        self.assertEqual([x[0] for x in statuses], ['missing'] * 5)
        self.assertEqual(episode[0], 'incomplete')
        self.assertEqual(stats['complete'], 0)
        self.assertEqual(stats['incomplete'], 1)
        self.assertIsNone(paper.record(example(), self.path, self.start + 4000))

    def test_unclosed_episode_requires_full_observation(self):
        paper.record(example(), self.path, self.start)
        for minute in paper.HORIZONS:
            with patch.object(paper, 'executable_sample', return_value=sample('0.5')):
                paper.poll(app, self.path, self.start + minute * 60)
        with paper.session(self.path) as db:
            self.assertEqual(db.execute('SELECT status FROM episodes').fetchone()[0], 'not_closed_60m')
            self.assertEqual(paper.statistics_for(db)['not_closed_60m_pct'], 100.0)

    def test_checkpoint_uses_orderbook_not_last_price(self):
        episode = {'symbol': 'ABCUSDT', 'spot_exchange': 'Binance',
                   'futures_exchange': 'Gate', 'quantity': '5',
                   'cost_snapshot_json': '{"multiplier": "1"}'}
        with patch.object(app, 'orderbook', return_value=([['10', '2'], ['11', '4']], [])), \
             patch.object(app.basis, 'futures_book', return_value=([], [['12', D('1')], ['11', D('4')]])):
            value = paper.executable_sample(app, episode)
        self.assertEqual(value['spot_vwap'], D('10.6'))
        self.assertEqual(value['futures_vwap'], D('11.2'))
        self.assertLess(value['executable_spread_pct'], value['raw_spread_pct'])


if __name__ == '__main__':
    unittest.main()
