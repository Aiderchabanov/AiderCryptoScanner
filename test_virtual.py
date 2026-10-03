import json
import tempfile
import time
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import MagicMock,patch
import app,basis,paper,virtual
from test_paper import example

class VirtualTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'test.sqlite'
        self.api=MagicMock();self.api.basis=basis;self.api.dec=app.dec
        self.api.sell_for_usdt=app.sell_for_usdt
        self.api.orderbook.return_value=([['10.01','100']], [['10','100']])
        self.future=patch.object(basis,'futures_book',return_value=([['10.11','100']],[['10.1','100']])).start()
        self.addCleanup(patch.stopall)
        patch.object(basis,'futures_fee',return_value=D('.001')).start()
        self.funding=patch.object(basis,'fresh_funding',return_value=(D('.0001'),time.time()+18000)).start()
        self.api.fee.return_value=D('.001')
        item=example();item['paper_budget_usdt']=D(50);item['next_funding_at']=time.time()+18000
        item['raw_spread_pct']=D(2);item['executable_spread_pct']=D('1.8')
        self.item=item;self.ident=paper.record(item,self.path)
    def quote(self,spread):
        return {'at':time.time(),'spread':str(spread),'spot_bid':'10','futures_ask':'10.1','spot_exit':'10','future_exit':'10.1','fees':'.1514','trading_net':'.2486','net_pnl':'.2486','funding_status':'no settlement crossed'}
    def state(self):
        with paper.session(self.path) as db:return dict(db.execute('SELECT * FROM virtual_state WHERE episode_id=?',(self.ident,)).fetchone())
    def test_exit_bid_ask_matched_quantity_actual_entry_fees(self):
        q=virtual.quote(self.api,virtual.load(self.ident,self.path))
        self.assertEqual(D(q['trading_net']),D('50')+D('50.9')-D('50.55')-D('50')-(D('.0509')+D('.05')+D('.05055')))
        self.assertEqual(q['spot_exit'],'10');self.assertEqual(q['future_exit'],'10.11')
        self.api.orderbook.assert_called_with('Binance','HBARUSDT')
    def test_missing_quotes_fee_or_depth_never_closes(self):
        for book,fee in (([],D('.001')),(([['10','1']],[['9.9','1']]),D('.001')),(([['10.1','100']],[['10','100']]),None)):
            self.api.orderbook.return_value=book;self.api.fee.return_value=fee
            with self.assertRaises(Exception):virtual.quote(self.api,virtual.load(self.ident,self.path))
        self.assertEqual(self.state()['state'],'open')
    def test_funding_after_settlement_unknown_not_zero(self):
        e=virtual.load(self.ident,self.path);e['next_funding_at']=time.time()-1
        q=virtual.quote(self.api,e)
        self.assertIsNone(q['net_pnl']);self.assertIn('неизвестен',virtual.pnl_line(q))
    def test_movement_levels_restart_no_duplicate_and_no_auto_average(self):
        with patch.object(virtual,'quote',return_value=self.quote('12')):
            virtual.observe(self.api,self.path)
            count=self.api.telegram.call_count
            virtual.bootstrap(self.path)
            virtual.observe(self.api,self.path)
        self.assertEqual(count,3) # 1.5pp notice and +5/+10 warnings
        self.assertEqual(self.api.telegram.call_count,count)
        self.assertEqual(self.state()['last_warning'],2)
        self.assertEqual(len(virtual.rows(self.path)),1)
    def test_strong_warning_becomes_latest_notification_baseline(self):
        with paper.session(self.path) as db:
            db.execute("UPDATE virtual_state SET last_notice_spread='6.7' WHERE episode_id=?",(self.ident,))
        with patch.object(virtual,'quote',return_value=self.quote('7')):
            virtual.observe(self.api,self.path)
        self.assertEqual(self.state()['last_notice_spread'],'7')
        self.assertEqual(self.api.telegram.call_count,1)

    def test_observe_close_and_capital_release_once(self):
        with patch.object(virtual,'quote',return_value=self.quote('.08')):
            virtual.observe(self.api,self.path);virtual.observe(self.api,self.path)
        self.assertEqual(self.state()['state'],'closed');self.assertEqual(self.api.telegram.call_count,1)
        with paper.session(self.path) as db:self.assertEqual(virtual.used(db),0)
        self.assertIsNotNone(paper.record(self.item,self.path))
    def test_negative_entry_not_closed_until_zero(self):
        with paper.session(self.path) as db:
            db.execute("UPDATE episodes SET executable_spread_pct='-1' WHERE id=?",(self.ident,))
        with patch.object(virtual,'quote',return_value=self.quote('-1')):virtual.observe(self.api,self.path)
        self.assertEqual(self.state()['state'],'open')
        with patch.object(virtual,'quote',return_value=self.quote('0')):virtual.observe(self.api,self.path)
        self.assertEqual(self.state()['state'],'closed')
    def test_close_two_confirmations_fresh_replay_and_restart(self):
        with patch.object(virtual,'quote',return_value=self.quote('1')):
            token=virtual.preview(self.api,'close',self.ident,'123','123',self.path)
            self.assertEqual(self.state()['state'],'open')
            virtual.bootstrap(self.path)
            with self.assertRaises(ValueError):virtual.confirm(self.api,token,'123','999',self.path)
            virtual.confirm(self.api,token,'123','123',self.path)
            with self.assertRaises(ValueError):virtual.confirm(self.api,token,'123','123',self.path)
        self.assertEqual(self.state()['state'],'closed')
        self.assertIsNone(paper.record(self.item,self.path)) # continuous gap manual-close latch
    def test_old_quote_expiry_price_change_no_close(self):
        with patch.object(virtual,'quote',return_value=self.quote('1')):
            token=virtual.preview(self.api,'close',self.ident,'123','123',self.path)
        with patch.object(virtual,'quote',side_effect=ValueError('offline')):
            with self.assertRaises(ValueError):virtual.confirm(self.api,token,'123','123',self.path)
        with patch.object(virtual,'quote',return_value=self.quote('1.3')):
            with self.assertRaises(ValueError):virtual.confirm(self.api,token,'123','123',self.path)
        with paper.session(self.path) as db:db.execute('UPDATE virtual_proposals SET expires_at=0')
        with self.assertRaises(ValueError):virtual.confirm(self.api,token,'123','123',self.path)
        self.assertEqual(self.state()['state'],'open')
    def test_open_close_commands_and_unauthorized(self):
        for text in ('/open','/close'):
            update={'message':{'chat':{'id':123},'from':{'id':123},'text':text}}
            self.assertTrue(virtual.handle(self.api,update,123,self.path))
        self.assertEqual(self.api.telegram.call_count,2)
        self.assertIn('inline_keyboard',self.api.telegram.call_args.kwargs['reply_markup'])
        self.assertFalse(virtual.handle(self.api,{'callback_query':{'data':f'close:{self.ident}','from':{'id':999},'message':{'chat':{'id':123}}}},123,self.path))
    def test_add_preview_confirm_once_and_budget_atomic(self):
        with patch.object(virtual,'quote',return_value=self.quote('7')):
            virtual.observe(self.api,self.path)
            with patch.object(virtual,'verified_entry',return_value=dict(self.item,executable_spread_pct=D('7'))):
                token=virtual.preview(self.api,'add',self.ident,'123','123',self.path,1)
                self.assertEqual(len(virtual.rows(self.path)),1)
                virtual.confirm(self.api,token,'123','123',self.path)
                self.assertEqual(len(virtual.rows(self.path)),2)
                with self.assertRaises(ValueError):virtual.confirm(self.api,token,'123','123',self.path)
                with self.assertRaises(ValueError):virtual.preview(self.api,'add',self.ident,'123','123',self.path,1)
        with paper.session(self.path) as db:
            self.assertEqual(virtual.used(db),D('201.8'))
            self.assertEqual(db.execute('SELECT parent_id FROM virtual_state ORDER BY episode_id DESC').fetchone()[0],self.ident)
        self.assertIsNone(paper.record(dict(self.item,symbol='XYZUSDT'),self.path))
    def test_unknown_negative_funding_and_anomaly_repeat(self):
        e=virtual.load(self.ident,self.path)
        with patch.object(basis,'evaluate',return_value=dict(self.item,pct=D(1),category='POSITIVE')),patch.object(basis,'fresh_funding',return_value=(D('-0.001'),time.time()+3600)):
            with self.assertRaises(ValueError):virtual.verified_entry(self.api,e)
        large=dict(self.item,pct=D(1),category='POSITIVE',raw_spread_pct=D(6),executable_spread_pct=D(6))
        with patch.object(basis,'evaluate',side_effect=[large,dict(large,executable_spread_pct=D('5.7'))]):
            with self.assertRaises(ValueError):virtual.verified_entry(self.api,e)
        with patch.object(basis,'evaluate',return_value=large) as evaluate,patch.object(basis,'fresh_funding',return_value=(D('.001'),time.time()+3600)):
            verified=virtual.verified_entry(self.api,e)
            self.assertEqual(evaluate.call_count,2);self.assertEqual(verified['anomaly'],'ANOMALOUS_SPREAD_RECONFIRMED')
    def test_continuous_gap_and_additional_entries_no_new_scan_duplicate(self):
        self.assertIsNone(paper.record(self.item,self.path))
        self.assertIsNotNone(paper.record(self.item,self.path,parent_id=self.ident))
        self.assertIsNone(paper.record(self.item,self.path))
    def test_funding_warning_fresh_books_once_and_restart(self):
        deadline=time.time()+1800
        self.funding.return_value=(D('.0001'),deadline)
        self.api.fee.return_value=None # Exit P&L costs do not suppress the funding reminder.
        virtual.funding_warnings(self.api,self.path)
        self.assertEqual(self.api.telegram.call_count,1)
        text=self.api.telegram.call_args.args[0]
        self.assertIn('FUNDING ЧЕРЕЗ 30 МИНУТ',text)
        self.assertIn('Spot Ask: 10.01; Futures Bid: 10.1',text)
        self.assertIn('Funding: +0.0100%',text)
        self.assertIn('до $50',text)
        virtual.bootstrap(self.path)
        virtual.funding_warnings(self.api,self.path)
        self.assertEqual(self.api.telegram.call_count,1)
        self.assertEqual(self.state()['state'],'open')
        with paper.session(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM virtual_events WHERE state='sent'").fetchone()[0],1)
            self.assertEqual(db.execute('SELECT next_funding_at FROM episodes').fetchone()[0],self.item['next_funding_at'])
        self.funding.return_value=(D('.0002'),deadline-1)
        virtual.funding_warnings(self.api,self.path)
        self.assertEqual(self.api.telegram.call_count,2) # Independent funding event key.

    def test_funding_warning_only_open_and_within_window(self):
        for deadline in (time.time()+1801,time.time()-1):
            self.funding.return_value=(D('.001'),deadline)
            virtual.funding_warnings(self.api,self.path)
        self.assertEqual(self.api.telegram.call_count,0)
        with paper.session(self.path) as db:db.execute("UPDATE virtual_state SET state='closed'")
        self.funding.return_value=(D('.001'),time.time()+1500)
        virtual.funding_warnings(self.api,self.path)
        self.assertEqual(self.api.telegram.call_count,0)

    def test_funding_unknown_or_no_fresh_depth_never_notifies(self):
        self.funding.return_value=(None,time.time()+1700)
        virtual.funding_warnings(self.api,self.path)
        self.funding.return_value=(D('.001'),time.time()+1700)
        self.api.orderbook.return_value=([['10.01','1']],[['10','1']])
        virtual.funding_warnings(self.api,self.path)
        self.assertEqual(self.api.telegram.call_count,0)
        with paper.session(self.path) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM virtual_events').fetchone()[0],0)

    def test_funding_warning_close_race_skips_send(self):
        q={'at':time.time(),'next_at':time.time()+1200,'rate':'.0001','spot_ask':'10','future_bid':'10.1','spot_vwap':'10','future_vwap':'10.1','raw_spread':'1','spread':'1'}
        def close_during_fetch(*args):
            with paper.session(self.path) as db:db.execute("UPDATE virtual_state SET state='closed'")
            return q
        with patch.object(virtual,'funding_snapshot',side_effect=close_during_fetch):
            virtual.funding_warnings(self.api,self.path)
        self.assertEqual(self.api.telegram.call_count,0)

    def test_funding_ambiguous_send_is_not_repeated_after_restart(self):
        self.funding.return_value=(D('.001'),time.time()+1500)
        self.api.telegram.side_effect=RuntimeError('network timeout')
        virtual.funding_warnings(self.api,self.path)
        virtual.bootstrap(self.path)
        virtual.funding_warnings(self.api,self.path)
        self.assertEqual(self.api.telegram.call_count,1)
        with paper.session(self.path) as db:self.assertEqual(db.execute('SELECT state FROM virtual_events').fetchone()[0],'claimed')

    def test_postgres_schema_lock_precedes_ddl(self):
        db=MagicMock(); db.is_postgres=True
        virtual.ensure(db)
        self.assertIn('virtual-schema', db.execute.call_args_list[0].args[0])
        self.assertIn('CREATE TABLE', db.execute.call_args_list[1].args[0])

    def test_update_offset_restart_dedup(self):
        self.assertTrue(virtual.claim_update(42,self.path));self.assertFalse(virtual.claim_update(42,self.path));self.assertEqual(virtual.cursor(self.path),43)
    def test_checkpoints_do_not_release_managed_position_after_60m(self):
        with patch.object(paper,'executable_sample',return_value={'spot_ask':D(10),'spot_vwap':D(10),'futures_bid':D(10),'futures_vwap':D(10),'raw_spread_pct':D(0),'executable_spread_pct':D(0)}):
            paper.poll(self.api,self.path,at=time.time()+3600)
        self.assertEqual(self.state()['state'],'open')
        with paper.session(self.path) as db:self.assertIsNone(db.execute('SELECT first_close_min FROM episodes WHERE id=?',(self.ident,)).fetchone()[0])

if __name__=='__main__':unittest.main()
