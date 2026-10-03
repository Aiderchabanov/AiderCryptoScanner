import json
import unittest
from decimal import Decimal as D
from unittest.mock import patch
import paper,virtual
import test_virtual

class AccountingTests(unittest.TestCase):
    setUp=test_virtual.VirtualTests.setUp
    state=test_virtual.VirtualTests.state
    quote=test_virtual.VirtualTests.quote

    def complete_quote(self,value):
        q=self.quote('.08')
        q.update(spot_pnl=value,futures_pnl='0',funding_realized_usdt='0',total_trading_fees='.1',net_pnl=value,trading_net=value)
        return q

    def test_signed_closed_sum_restart_open_excluded(self):
        for value in ('.70','-.15','1.10'):
            with paper.session(self.path) as db:
                e=virtual.load(self.ident,self.path)
                virtual.close_db(db,e,self.complete_quote(value))
            self.ident=paper.record(self.item,self.path)
        before=virtual.accounting(self.path)
        self.assertEqual(before['cumulative_realized_pnl'],D('1.65'))
        self.assertEqual(before['current_virtual_balance'],D('501.65'))
        self.assertEqual(before['closed_complete'],3)
        self.assertEqual(before['open_episodes'],1)
        virtual.bootstrap(self.path)
        self.assertEqual(virtual.accounting(self.path),before)
        with patch.object(virtual,'quote',return_value=self.complete_quote('12')):
            a=virtual.accounting(self.path,self.api)
        self.assertEqual(a['unrealized_pnl_open'],D(12))
        self.assertEqual(a['current_virtual_balance'],D('501.65'))

    def test_unknown_funding_and_old_close_incomplete(self):
        q=self.complete_quote('.70');q.update(net_pnl=None,funding_realized_usdt=None,funding_status='UNKNOWN_SETTLEMENT_PNL')
        with paper.session(self.path) as db:
            virtual.close_db(db,virtual.load(self.ident,self.path),q)
        stored=json.loads(self.state()['close_json'])
        self.assertIsNone(stored['realized_net_pnl_usdt'])
        self.assertEqual(stored['funding_status'],'UNKNOWN_SETTLEMENT_PNL')
        a=virtual.accounting(self.path)
        self.assertEqual(a['incomplete_closed_pnl_count'],1)
        self.assertEqual(a['current_virtual_balance'],500)
        self.assertEqual(a['used_capital'],0)
        with paper.session(self.path) as db:
            db.execute('UPDATE virtual_state SET close_json=?',(json.dumps(self.complete_quote('9')),))
        self.assertEqual(virtual.accounting(self.path)['incomplete_closed_pnl_count'],1)

    def test_saved_legacy_complete_normalized_without_inventing_data(self):
        with paper.session(self.path) as db:
            db.execute("UPDATE virtual_state SET state='closed',close_json=?",(json.dumps(self.complete_quote('-.15')),))
        virtual.bootstrap(self.path)
        a=virtual.accounting(self.path)
        self.assertEqual(a['confirmed_realized_pnl'],D('-.15'))
        self.assertEqual(a['closed_complete_ids'],[self.ident])
        virtual.bootstrap(self.path)
        self.assertEqual(virtual.accounting(self.path),a)
        q=self.complete_quote('.7');q.update(net_pnl=None,funding_realized_usdt=None,funding_status='UNKNOWN_SETTLEMENT_PNL')
        with paper.session(self.path) as db:
            db.execute('UPDATE virtual_state SET close_json=?',(json.dumps(q),))
        virtual.bootstrap(self.path)
        self.assertEqual(virtual.accounting(self.path)['closed_incomplete_ids'],[self.ident])
        self.assertIsNone(json.loads(self.state()['close_json'])['realized_net_pnl_usdt'])

    def test_missing_mandatory_component_blocks_realized(self):
        for key in ('spot_pnl','futures_pnl','funding_realized_usdt','total_trading_fees','net_pnl'):
            q=self.complete_quote('.7');q.pop(key)
            self.assertIsNone(virtual.realized_result(q))

    def test_auto_and_manual_close_same_realized(self):
        q=self.complete_quote('-.15')
        with patch.object(virtual,'quote',return_value=q):virtual.observe(self.api,self.path)
        self.assertEqual(json.loads(self.state()['close_json'])['realized_net_pnl_usdt'],'-0.15')
        self.ident=paper.record(self.item,self.path)
        q=self.complete_quote('.7');q['spread']='1'
        with patch.object(virtual,'quote',return_value=q):
            token=virtual.preview(self.api,'close',self.ident,'123','123',self.path)
            virtual.confirm(self.api,token,'123','123',self.path)
        self.assertEqual(virtual.accounting(self.path)['confirmed_realized_pnl'],D('.55'))

    def test_status_safe_and_unknown_unrealized(self):
        with patch.object(virtual,'quote',side_effect=ValueError('unavailable')):
            self.assertTrue(virtual.handle(self.api,{'message':{'text':'/status','chat':{'id':123},'from':{'id':123}}},123,self.path))
        message=self.api.telegram.call_args.args[0]
        self.assertIn('Current virtual balance: 500.00',message)
        self.assertIn('Unrealized P&L open: UNKNOWN',message)
        self.assertEqual(self.state()['state'],'open')
        self.assertFalse(virtual.handle(self.api,{'message':{'text':'/status','chat':{'id':123},'from':{'id':999}}},123,self.path))
