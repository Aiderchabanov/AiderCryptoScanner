"""Isolated mocks: no live exchange calls or production episodes."""
import copy
import time
import unittest
from decimal import Decimal as D
from unittest.mock import Mock, patch
import basis, virtual, paper, entry_diagnostics as diag
from test_paper import example

class RevalidationTests(unittest.TestCase):
    def item(self):
        return dict(example(),symbol='ABCUSDT',spot='Gate',future='MEXC',pct=D('1'),projected=D('.3'),funding=D('.001'),next_funding_at=time.time()+3600,raw_spread_pct=D('2'),executable_spread_pct=D('2'))
    def scan(self,old,new=None,error=None):
        api=Mock();api.multi_exchange=True;api.dec=D;api.credentials_ready.return_value=True
        api.market_maps.return_value=({'Gate':{'ABCUSDT':{'lowest_ask':'10'}}},{'MEXC':{'ABCUSDT':{'bidPrice':'11'}}})
        with patch.object(paper,'storage_ready',return_value=True),patch.object(basis,'evaluate',return_value=old),patch.object(basis,'api_blocked_until',{}),patch.object(virtual,'verified_entry',return_value=new,side_effect=error) as verify,patch.object(basis,'fresh_funding') as funding,patch.object(paper,'record',return_value=None) as record,patch.object(diag,'completed'),patch.object(diag,'flow',wraps=diag.flow) as flow:
            result=basis.scan(api)
        funding.assert_not_called()
        return result,verify,record,flow
    def test_stale_original_replaced_all_fields(self):
        old=self.item();old['verified_at']=time.time()-400;before=copy.deepcopy(old)
        new=dict(old,verified_at=time.time(),spot_entry=D('12'),future_entry=D('13'),quantity=D('2'),spot_cost=D('24'),future_notional=D('26'),pct=D('.9'),projected=D('.216'))
        result,verify,record,flow=self.scan(old,new)
        self.assertEqual(result,[old]);self.assertEqual(old,before)
        self.assertIs(record.call_args.args[0],new);verify.assert_called_once()
        stages=[c.args[0] for c in flow.call_args_list]
        self.assertEqual(stages.count('ENTRY_GATE_PASSED'),1)
        self.assertEqual(stages.count('ENTRY_REVALIDATION_PASSED'),1)
    def test_failure_no_fallback(self):
        for exc in (ValueError('depth'),TimeoutError('cooldown'),ValueError('stale')):
            with self.subTest(exc=type(exc).__name__):
                _,verify,record,flow=self.scan(self.item(),error=exc)
                record.assert_not_called();verify.assert_called_once()
                self.assertIn('ENTRY_REVALIDATION_FAILED',[c.args[0] for c in flow.call_args_list])
    def test_refreshed_net_below_gate_no_record(self):
        old=self.item();new=dict(old,pct=D('.1'))
        _,_,record,_=self.scan(old,new);record.assert_not_called()
    def helper(self,item,funding=None,elapsed=False):
        api=Mock();api.basis=basis
        with patch.object(basis,'evaluate',return_value=item) as evaluate,patch.object(basis,'fresh_funding',return_value=funding or (D('.001'),time.time()+3600)) as fund,patch.object(virtual.time,'monotonic',side_effect=[0,31] if elapsed else [0,1]):
            value=virtual.verified_entry(api,{'symbol':'ABCUSDT','spot_exchange':'Gate','futures_exchange':'MEXC'})
        return value,evaluate,fund
    def test_helper_final_funding_once_and_trace_context(self):
        token=diag.begin()
        try:
            start=diag.CYCLE.get()['started_at'];value,evaluate,fund=self.helper(self.item())
            evaluate.assert_called_once();fund.assert_called_once()
            events=diag.CYCLE.get()['flow_events']
            self.assertEqual([x['stage'] for x in events],['FINAL_FUNDING_RECHECK_ATTEMPT','FINAL_FUNDING_RECHECK_PASSED'])
            self.assertTrue(all(x['scan_started_at']==start and x['direction']=='Gate->MEXC' for x in events))
        finally:diag.CYCLE.reset(token)
    def test_funding_nonpositive_no_item(self):
        with self.assertRaises(ValueError):self.helper(self.item(),(D('0'),time.time()+3600))
    def test_duration_guard_unchanged(self):
        with self.assertRaises(ValueError):self.helper(self.item(),elapsed=True)
    def test_missing_evaluate_no_funding(self):
        api=Mock();api.basis=basis
        with patch.object(basis,'evaluate',return_value=None),patch.object(basis,'fresh_funding') as funding:
            with self.assertRaises(ValueError):virtual.verified_entry(api,{'symbol':'ABCUSDT','spot_exchange':'Gate','futures_exchange':'MEXC'})
            funding.assert_not_called()
    def test_anomaly_repeat_preserved(self):
        item=dict(self.item(),raw_spread_pct=D(6),executable_spread_pct=D(6))
        value,evaluate,fund=self.helper(item)
        self.assertEqual(evaluate.call_count,2);fund.assert_called_once()
        self.assertEqual(value['anomaly'],'ANOMALOUS_SPREAD_RECONFIRMED')
    def test_selection_unchanged_no_replacement(self):
        api=Mock();api.multi_exchange=True;api.dec=D;api.credentials_ready.return_value=True
        names=['AAAUSDT','BBBUSDT','CCCUSDT','DDDUSDT']
        api.market_maps.return_value=({'Gate':{s:{'lowest_ask':'10'} for s in names}},{'MEXC':{s:{'bidPrice':'11'} for s in names}})
        def evaluate(api,symbol,*args):return dict(self.item(),symbol=symbol,pct=D(4-names.index(symbol)))
        with patch.object(paper,'storage_ready',return_value=True),patch.object(basis,'evaluate',side_effect=evaluate),patch.object(basis,'api_blocked_until',{}),patch.object(virtual,'verified_entry',side_effect=ValueError('changed net')) as verify,patch.object(paper,'record') as record,patch.object(diag,'completed'):
            basis.scan(api)
        self.assertEqual([c.args[1]['symbol'] for c in verify.call_args_list],names[:3]);record.assert_not_called()
    def test_diagnostic_write_failure_keeps_success(self):
        token=diag.begin()
        try:
            with patch.object(diag.logging,'info',side_effect=RuntimeError('log unavailable')):
                value,_,_=self.helper(self.item())
            self.assertEqual(value['pct'],D(1))
        finally:diag.CYCLE.reset(token)
    def test_negative_refreshed_funding_no_record(self):
        _,_,record,_=self.scan(self.item(),dict(self.item(),funding=D('-0.001')))
        record.assert_not_called()
