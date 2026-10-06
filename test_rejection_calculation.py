import copy
import json
import sqlite3
import unittest
from decimal import Decimal
from unittest.mock import patch
import entry_diagnostics as d
import entry_policy


class RejectionCalculationTests(unittest.TestCase):
    def test_unnamed_no_entry_does_not_invent_policy_reason_or_counter(self):
        import paper
        token=d.begin()
        try:
            with patch.object(paper,'storage_ready',return_value=False),patch.object(d.logging,'info') as log:
                d.observe_no_entry(dict(symbol='ABCUSDT',spot='Gate',future='MEXC'))
            self.assertEqual(log.call_args.args[1].find('"reject_reason": "UNKNOWN"')>=0,True)
            self.assertEqual(dict(d.CYCLE.get()['counts']),{})
        finally:d.CYCLE.reset(token)
    def test_evaluate_same_decision_and_requests_with_diagnostics(self):
        import app,basis,paper
        from contextlib import ExitStack
        metadata={'step':Decimal('0.1'),'min_qty':Decimal('0.1'),'min_notional':Decimal('5'),'multiplier':Decimal('0.1')}
        with ExitStack() as stack:
            stack.enter_context(patch.object(basis,'futures_meta',return_value=metadata))
            stack.enter_context(patch.object(basis,'futures_fee',return_value=Decimal('0.001')))
            fee=stack.enter_context(patch.object(app,'fee',return_value=Decimal('0.001')))
            book=stack.enter_context(patch.object(app,'orderbook',return_value=([['10','100']],[['9.99','100']])))
            fut=stack.enter_context(patch.object(basis,'futures_book',return_value=([['10.02','100']],[['10.01','100']])))
            rules=stack.enter_context(patch.object(app,'spot_rules',return_value={'min_qty':Decimal('0.1'),'min_quote':Decimal('5'),'step':Decimal('0.01')}))
            funding=stack.enter_context(patch.object(basis,'fresh_funding',return_value=(Decimal('0.001'),9999999999)))
            network=stack.enter_context(patch.object(app.requests,'get',side_effect=AssertionError('extra exchange request')))
            episode=stack.enter_context(patch.object(paper,'record',side_effect=AssertionError('episode created')))
            stack.enter_context(patch.object(basis.time,'time',return_value=1000))
            with patch.object(d,'capture'),patch.object(d,'capture_book'):
                baseline=basis.evaluate(app,'ABCUSDT','Binance','Gate')
            actual=basis.evaluate(app,'ABCUSDT','Binance','Gate')
            self.assertEqual(entry_policy.reason(actual,1000),entry_policy.reason(baseline,1000))
            self.assertEqual({k:v for k,v in actual.items() if k!='entry_calculation'}, {k:v for k,v in baseline.items() if k!='entry_calculation'})
            for request in (fee,book,fut,rules,funding):self.assertEqual(request.call_count,2)
            self.assertEqual(actual['entry_calculation']['spot_depth_usdt'],Decimal('1000'))
            self.assertEqual(actual['entry_calculation']['spot_quantity'],Decimal('2.91'))
            network.assert_not_called();episode.assert_not_called()

    def test_allowlist_preserves_unknown_and_filters_secrets(self):
        row=d.rejection_snapshot('REJECTED_LOW_EXPECTED_NET_RETURN',dict(
            symbol='ABCUSDT',spot='Gate',future='MEXC',pct='NaN',spot_fee=None,
            signature='SECRET',api_key='SECRET',entry_calculation={'secret':'SECRET','spot_ask':'1.01'}))
        self.assertIsNone(row['pct'])
        self.assertIsNone(row['spot_fee'])
        self.assertIsNone(row['funding'])
        self.assertEqual(row['spot_ask'],'1.01')
        self.assertNotIn('SECRET',json.dumps(row))
        self.assertFalse(row['expected_net_known'])

    def test_snapshot_does_not_change_item_or_entry_decision(self):
        item=dict(symbol='ABCUSDT',spot='Gate',future='MEXC',pct=Decimal('0.1'),
                  funding=Decimal('0.001'),next_funding_at=9999999999,
                  entry_calculation={'spot_quantity':Decimal('20')})
        original=copy.deepcopy(item)
        before=entry_policy.reason(item)
        d.rejection_snapshot(before,item)
        self.assertEqual(item,original)
        self.assertEqual(entry_policy.reason(item),before)

    def test_persist_uses_existing_metadata_only_and_no_episode(self):
        db=sqlite3.connect(':memory:')
        db.execute('CREATE TABLE virtual_meta (name TEXT PRIMARY KEY,value TEXT NOT NULL)')
        row=d.rejection_snapshot('REJECTED_NON_POSITIVE_NET_ENTRY',dict(symbol='ABCUSDT',spot='Gate',future='MEXC',pct='-1'))
        d.persist_rejection(db,row)
        saved=json.loads(db.execute('SELECT value FROM virtual_meta').fetchone()[0])
        self.assertEqual(saved,row)
        self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),[('virtual_meta',)])

    def test_top10_known_values_only_sorted_by_distance(self):
        token=d.begin()
        try:
            with patch.object(d.logging,'info'):
                for i in range(15):
                    d.retain_rejection(d.rejection_snapshot('REJECTED_LOW_EXPECTED_NET_RETURN',dict(symbol='ABCUSDT',spot='Gate',future='MEXC',pct=Decimal(i)/100)))
                d.retain_rejection(d.rejection_snapshot('REJECTED_UNKNOWN_FUNDING',dict(symbol='UNKNOWN',spot='Gate',future='MEXC')))
            rows=d.CYCLE.get()['top_rejections']
            self.assertEqual(len(rows),10)
            self.assertEqual(rows[0]['pct'],'0.14')
            self.assertTrue(all(x['pct'] is not None for x in rows))
        finally:d.CYCLE.reset(token)

    def test_partial_capture_isolated_and_no_external_calls(self):
        token=d.CALCULATION.set({})
        try:
            d.capture(spot_ask=Decimal('2'),secret='SECRET')
            row=d.rejection_snapshot('REJECTED_MIN_ORDER_ABOVE_TARGET',dict(symbol='ABCUSDT',spot='Gate',future='MEXC'))
            self.assertEqual(row['spot_ask'],'2')
            self.assertIsNone(row['future_entry'])
            self.assertNotIn('secret',row)
        finally:d.CALCULATION.reset(token)
        self.assertIsNone(d.CALCULATION.get())

    def test_diagnostic_failure_does_not_change_reject_counter(self):
        import paper
        with patch.object(d,'rejection_snapshot',side_effect=RuntimeError('unavailable')), patch.object(paper,'storage_ready',return_value=False):
            self.assertIsNone(entry_policy.reject('REJECTED_NON_POSITIVE_NET_ENTRY',dict(symbol='ABCUSDT')))

    def test_real_zero_is_distinct_from_unknown(self):
        row=d.rejection_snapshot('REJECTED_LOW_EXPECTED_NET_RETURN',dict(symbol='ABCUSDT',spot='Gate',future='MEXC',pct='0',spot_fee='0',future_fee=None))
        self.assertEqual(row['spot_fee'],'0')
        self.assertIsNone(row['future_fee'])
        self.assertTrue(row['expected_net_known'])
