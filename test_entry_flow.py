import copy
import json
import unittest
from contextlib import ExitStack
from decimal import Decimal as D
from unittest.mock import Mock, patch
import basis, paper, entry_diagnostics as d
import test_virtual


class EvaluateFlowTests(unittest.TestCase):
    def evaluate(self,cost='29.1',proceeds='29.29',order_ok=True):
        api=Mock();api.multi_exchange=False;api.dec=D;api.order_size_ok.return_value=order_ok
        api.fee.return_value=D('.001');api.orderbook.return_value=([['10','100']],[['9.99','100']])
        api.sell_for_usdt.return_value=D(proceeds)
        meta=dict(step=D('.1'),min_qty=D('.1'),min_notional=D('5'),multiplier=D('.1'))
        api.spot_rules.return_value=dict(step=D('.01'),min_qty=D('.1'),min_quote=D('5'))
        with ExitStack() as stack:
            stack.enter_context(patch.object(basis,'futures_meta',return_value=meta))
            stack.enter_context(patch.object(basis,'futures_fee',return_value=D('.001')))
            stack.enter_context(patch.object(basis,'futures_book',return_value=([['10.2','100']],[['10.1','100']])))
            stack.enter_context(patch.object(basis,'spot_cost',return_value=D(cost)))
            stack.enter_context(patch.object(basis,'fresh_funding',return_value=(D('.001'),9999999999)))
            stack.enter_context(patch.object(paper,'storage_ready',return_value=False))
            with patch.object(d,'observe_no_entry',wraps=d.observe_no_entry),self.assertLogs(level='INFO') as logs:
                result=basis.evaluate(api,'ABCUSDT','Gate','MEXC')
        calc=[json.loads(x.split('Entry rejection calculation: ')[1]) for x in logs.output if 'Entry rejection calculation:' in x]
        api.telegram.assert_not_called()
        return result,calc,api

    def test_cost_above_target_same_none(self):
        result,rows,api=self.evaluate(cost='31')
        self.assertIsNone(result)
        self.assertEqual(rows[0]['silent_return_reason'],'EXECUTABLE_COST_OR_NOTIONAL_ABOVE_TARGET')
        self.assertEqual(rows[0]['reject_reason'],'UNKNOWN')
        api.orderbook.assert_called_once()

    def test_spread_sign_mismatch(self):
        result,rows,_=self.evaluate(cost='29.1',proceeds='28.9')
        self.assertIsNone(result)
        self.assertEqual(rows[0]['silent_return_reason'],'SPREAD_CATEGORY_MISMATCH')

    def test_order_size_false_distinct_from_under_80(self):
        result,rows,api=self.evaluate(order_ok=False)
        self.assertIsNone(result);self.assertFalse(rows[0]['order_size_ok'])
        self.assertEqual(rows[0]['silent_return_reason'],'ORDER_SIZE_NOT_OK')
        api.order_size_ok.assert_called_once()
        result,rows,api=self.evaluate(cost='20',proceeds='20')
        self.assertIsNone(result);self.assertTrue(rows[0]['order_size_ok'])
        self.assertEqual(rows[0]['silent_return_reason'],'LEG_BELOW_80_PERCENT_TARGET')
        api.order_size_ok.assert_called_once()

    def test_successful_entry_gate_and_downstream_without_episode(self):
        item=dict(symbol='ABCUSDT',spot='Gate',future='MEXC',pct=D('.3'),funding=D('.001'),next_funding_at=9999999999)
        api=Mock();api.multi_exchange=True;api.dec=D;api.credentials_ready.return_value=True
        api.market_maps.return_value=({'Gate':{'ABCUSDT':{'lowest_ask':'10'}}},{'MEXC':{'ABCUSDT':{'bidPrice':'11'}}})
        with patch.object(paper,'storage_ready',return_value=True),patch.object(basis,'api_blocked_until',{}),patch.object(basis,'evaluate',return_value=item),patch('virtual.verified_entry',return_value=item) as funding,patch.object(paper,'record',return_value=None),patch.object(d,'flow',wraps=d.flow) as flow,patch.object(d,'completed'):
            found=basis.scan(api)
        self.assertEqual(found,[item]);funding.assert_called_once()
        stages=[x.args[0] for x in flow.call_args_list]
        for stage in ('SHORTLIST_CANDIDATE','ENTRY_GATE_PASSED','DOWNSTREAM_SELECTED_FOR_ALERT','ENTRY_REVALIDATION_PASSED','FINAL_QUALIFIES_PASSED','PAPER_RECORD_ATTEMPT'):
            self.assertEqual(stages.count(stage),1)
        api.telegram.assert_not_called()

    def test_allowlist_unknown_and_no_mutation(self):
        token=d.begin();self.addCleanup(d.CYCLE.reset,token)
        item=dict(symbol='ABCUSDT',spot='Gate',future='MEXC',pct=None,api_key='SECRET',signature='SECRET')
        original=copy.deepcopy(item)
        d.flow('ENTRY_GATE_PASSED',item)
        row=d.CYCLE.get()['flow_events'][0]
        self.assertIsNone(row['pct']);self.assertIsNone(row['funding'])
        self.assertNotIn('SECRET',json.dumps(row));self.assertEqual(item,original)

    def test_diagnostic_persistence_failure_does_not_change_decision(self):
        token=d.begin();self.addCleanup(d.CYCLE.reset,token)
        d.flow('ENTRY_GATE_PASSED',dict(symbol='ABCUSDT',spot='Gate',future='MEXC',pct='.3'))
        with patch.object(paper,'storage_ready',return_value=True),patch.object(paper,'session',side_effect=RuntimeError('unavailable')):
            self.assertIsNone(d.completed(1,0))
        self.assertEqual(len(d.CYCLE.get()['flow_events']),1)


class RecordFlowTests(unittest.TestCase):
    setUp=test_virtual.VirtualTests.setUp
    state=test_virtual.VirtualTests.state

    def test_existing_open_preserved(self):
        before=self.state();token=d.begin()
        try:
            self.assertIsNone(paper.record(self.item,self.path))
            self.assertEqual(d.CYCLE.get()['flow_events'][-1]['reason'],'EXISTING_OPEN_EPISODE')
        finally:d.CYCLE.reset(token)
        self.assertEqual(self.state(),before)

    def test_capital_block_unchanged(self):
        item=dict(self.item,symbol='OTHERUSDT',paper_budget_usdt=D(30),spot_cost=D(30),future_notional=D(30))
        token=d.begin()
        try:
            with patch('virtual.used',return_value=D(500)),patch.object(basis,'LEG_USDT',D(30)),patch.object(paper,'storage_ready',return_value=False):
                self.assertIsNone(paper.record(item,self.path))
            blocked=[x for x in d.CYCLE.get()['flow_events'] if x['stage']=='PAPER_RECORD_BLOCKED']
            self.assertEqual(blocked[-1]['reason'],'MAX_WORKING_CAPITAL')
        finally:d.CYCLE.reset(token)
        self.assertEqual(self.state()['state'],'open')
