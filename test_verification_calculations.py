"""Diagnostic regressions only: mocks/local SQLite, no live requests."""
import copy
import json
import sqlite3
import time
import unittest
from contextlib import contextmanager
from decimal import Decimal as D
from unittest.mock import Mock, patch
import basis, virtual, paper, entry_diagnostics as diag
import test_entry_revalidation

class VerificationCalculationTests(unittest.TestCase):
    def setUp(self):
        self.token=diag.begin()
        self.addCleanup(diag.CYCLE.reset,self.token)
    def item(self, **changes):
        return dict(symbol='AIAUSDT',spot='Gate',future='MEXC',pct=D('.5'),projected=D('.15'),
                    funding=D('.00005'),next_funding_at=time.time()+3600,spot_cost=D('29'),
                    future_notional=D('29.5'),spot_entry=D('1'),future_entry=D('1.02'),
                    raw_spread_pct=D('2'),executable_spread_pct=D('2'),
                    entry_calculation={'quantity':D('29'),'spot_quantity':D('29.03'),
                        'protective_reserve_usdt':D('.058'),'spot_fee':D('.001')},**changes)
    def rows(self):return diag.CYCLE.get().get('verification_calculations',[])
    def helper(self,item):
        api=Mock();api.basis=basis
        with patch.object(basis,'evaluate',return_value=item) as ev,patch.object(basis,'fresh_funding',return_value=(D('.00005'),item['next_funding_at'])) as fund:
            result=virtual.verified_entry(api,{'symbol':'AIAUSDT','spot_exchange':'Gate','futures_exchange':'MEXC'})
        return result,ev,fund
    def test_original_and_new_separate_and_no_mutation_or_counters(self):
        old=self.item();new=self.item();new.update(pct=D('.8'),spot_entry=D('1.1'))
        before=copy.deepcopy((old,new));cycle=diag.CYCLE.get()
        diag.verification_snapshot('ORIGINAL_SELECTED_CALCULATION',old)
        self.helper(new)
        self.assertEqual((old,new),before)
        self.assertEqual([r['pct'] for r in self.rows()],['0.5','0.8'])
        self.assertEqual([r['spot_entry'] for r in self.rows()],['1','1.1'])
        self.assertEqual(cycle['top_rejections'],[]);self.assertEqual(dict(cycle['counts']),{})
        self.assertNotIn('calculation_records',cycle)
        self.assertTrue(all(r['scan_started_at']==cycle['started_at'] and r['direction']=='Gate->MEXC' for r in self.rows()))
    def test_negative_net_reason_and_no_final_funding(self):
        item=self.item();item['pct']=D('-.3')
        api=Mock();api.basis=basis
        with patch.object(basis,'evaluate',return_value=item),patch.object(basis,'fresh_funding') as f,patch('entry_policy.reject') as reject:
            with self.assertRaisesRegex(ValueError,'Entry filters failed'):virtual.verified_entry(api,{'symbol':'AIAUSDT','spot_exchange':'Gate','futures_exchange':'MEXC'})
        f.assert_not_called();reject.assert_not_called()
        self.assertEqual(self.rows()[0]['reject_reason'],'REJECTED_NON_POSITIVE_NET_ENTRY')
    def test_anomaly_repeat_distinct_same_calls(self):
        item=self.item();item.update(raw_spread_pct=D(6),executable_spread_pct=D(6))
        _,ev,fund=self.helper(item)
        self.assertEqual(ev.call_count,2);fund.assert_called_once()
        self.assertEqual([r['pass_name'] for r in self.rows()],['PRIMARY','ANOMALY_REPEAT'])
    def test_unknown_and_secrets_allowlist(self):
        item=self.item();item.update(pct=D('NaN'),secret='TOPSECRET',headers={'APIKEY':'TOPSECRET'},spot_cost=None)
        diag.verification_snapshot('ORIGINAL_SELECTED_CALCULATION',item)
        row=self.rows()[0]
        self.assertIsNone(row['pct']);self.assertIsNone(row['spot_cost']);self.assertIsNone(row['spot_slippage_usdt'])
        self.assertNotIn('TOPSECRET',json.dumps(row));self.assertEqual(row['quantity'],'29')
    def test_none_and_exception_no_fallback(self):
        for value in (None,ValueError('depth')):
            api=Mock();api.basis=basis
            with patch.object(basis,'evaluate',return_value=None,side_effect=value if isinstance(value,Exception) else None),patch.object(basis,'fresh_funding') as f:
                with self.assertRaises(ValueError):virtual.verified_entry(api,{'symbol':'AIAUSDT','spot_exchange':'Gate','futures_exchange':'MEXC'})
            f.assert_not_called()
        self.assertEqual(len(self.rows()),2);self.assertTrue(all(r['pct'] is None for r in self.rows()))
        self.assertEqual(self.rows()[1]['exception_type'],'ValueError')
    def test_logging_failure_keeps_success(self):
        with patch.object(diag.logging,'info',side_effect=RuntimeError('log unavailable')),patch.object(diag.logging,'warning',side_effect=RuntimeError('warning unavailable')):
            # Existing funding flow logs also use warning, so isolate only the new helper.
            diag.verification_snapshot('ORIGINAL_SELECTED_CALCULATION',self.item())
            diag.verification_rejected(self.item(),'PRIMARY')
        self.assertEqual(len(self.rows()),1)
        result,ev,fund=self.helper(self.item());self.assertEqual(result['pct'],D('.5'));ev.assert_called_once();fund.assert_called_once()
    def test_no_scan_context_no_snapshot(self):
        token=diag.CYCLE.set(None)
        try:
            diag.verification_snapshot('ORIGINAL_SELECTED_CALCULATION',self.item())
            diag.verification_rejected(self.item(),'PRIMARY')
        finally:diag.CYCLE.reset(token)
        self.assertEqual(self.rows(),[])
    def test_deferred_sql_storage_no_episode(self):
        db=sqlite3.connect(':memory:');self.addCleanup(db.close)
        db.execute('CREATE TABLE virtual_meta(name TEXT PRIMARY KEY,value TEXT)')
        @contextmanager
        def session():yield db
        with patch.object(paper,'session',side_effect=session) as sess,patch.object(paper,'storage_ready',return_value=True),patch.object(paper,'record') as record:
            diag.verification_snapshot('ORIGINAL_SELECTED_CALCULATION',self.item());sess.assert_not_called()
            diag.completed(1,0);record.assert_not_called()
        row=db.execute("SELECT value FROM virtual_meta WHERE name LIKE 'entry_verification_calculations:%'").fetchone()
        self.assertEqual(json.loads(row[0]),self.rows())
        self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),[('virtual_meta',)])
    def test_persistence_failure_keeps_flow_write_and_decision(self):
        real=sqlite3.connect(':memory:');self.addCleanup(real.close)
        real.execute('CREATE TABLE virtual_meta(name TEXT PRIMARY KEY,value TEXT)')
        db=Mock()
        def execute(sql,*args):
            if sql.startswith('INSERT') and args[0][0].startswith('entry_verification_calculations:'):raise sqlite3.OperationalError('write failed')
            return real.execute(sql,*args)
        db.execute.side_effect=execute
        @contextmanager
        def session():yield db
        item=self.item();result,_,_=self.helper(item)
        with patch.object(paper,'session',side_effect=session),patch.object(paper,'storage_ready',return_value=True),self.assertLogs(level='WARNING') as logs:
            diag.completed(1,0)
        self.assertIs(result,item)
        self.assertTrue(any('Entry verification persistence unavailable' in l for l in logs.output))
        self.assertIsNotNone(real.execute("SELECT value FROM virtual_meta WHERE name LIKE 'entry_flow:%'").fetchone())
    def test_scan_selected_snapshot_and_old_guards(self):
        fixture=test_entry_revalidation.RevalidationTests();old=fixture.item();new=fixture.item()
        _,verify,record,_=fixture.scan(old,new)
        verify.assert_called_once();record.assert_called_once_with(new)
        # basis.scan has its own CYCLE: intercept the snapshot at the real call site.
        with patch.object(diag,'verification_snapshot',wraps=diag.verification_snapshot) as snapshot:
            fixture.scan(old,new)
        self.assertEqual(snapshot.call_args.args[0],'ORIGINAL_SELECTED_CALCULATION')
        self.assertIs(snapshot.call_args.args[1],old)
