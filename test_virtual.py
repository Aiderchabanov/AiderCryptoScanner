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
        patch.object(basis,'LEG_USDT',D(50)).start() # Historical $50 episodes remain supported.
        patch.object(basis,'futures_fee',return_value=D('.001')).start()
        self.funding=patch.object(basis,'fresh_funding',return_value=(D('.0001'),time.time()+18000)).start()
        self.api.fee.return_value=D('.001')
        item=example();item['projected']=item['spot_cost']*item['pct']/100;item['paper_budget_usdt']=D(50);item['next_funding_at']=time.time()+18000
        item['raw_spread_pct']=D(2);item['executable_spread_pct']=D('1.8')
        self.item=item;self.ident=paper.record(item,self.path)
        # Existing lifecycle fixtures model legacy episodes, not the new policy.
        with paper.session(self.path) as db:
            row=db.execute('SELECT cost_snapshot_json FROM episodes WHERE id=?',(self.ident,)).fetchone()
            snap=json.loads(row['cost_snapshot_json']);snap.pop('entry_policy',None)
            db.execute('UPDATE episodes SET cost_snapshot_json=? WHERE id=?',(json.dumps(snap),self.ident))
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
    def test_auto_close_symmetric_band_all_entry_signs(self):
        for entry in ('1', '-1', '0'):
            for spread, expected in (('.08','closed'), ('-.08','closed'), ('.11','open'), ('-.11','open'), ('.10','closed'), ('-.10','closed')):
                with self.subTest(entry=entry, spread=spread):
                    with paper.session(self.path) as db:
                        db.execute('UPDATE episodes SET executable_spread_pct=? WHERE id=?',(entry,self.ident))
                        db.execute("UPDATE virtual_state SET state='open' WHERE episode_id=?",(self.ident,))
                        db.execute('UPDATE virtual_metrics SET convergence_seconds=NULL WHERE episode_id=?',(self.ident,))
                    with patch.object(virtual,'quote',return_value=self.quote(spread)):
                        virtual.observe(self.api,self.path)
                    self.assertEqual(self.state()['state'],expected)
                    with paper.session(self.path) as db:
                        m=db.execute('SELECT convergence_seconds FROM virtual_metrics WHERE episode_id=?',(self.ident,)).fetchone()
                        self.assertEqual(m['convergence_seconds'] is not None,expected=='closed')

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
    def test_actual_capital_leverage_limits_release_and_restart(self):
        for lev,count,per_episode in (('1',2,D(100)),('2',3,D(75)),('5',4,D(60))):
            path=Path(self.tmp.name)/f'leverage{lev}.sqlite'
            item=dict(self.item,future_notional=D(50))
            with patch.dict('os.environ',{'PAPER_FUTURES_LEVERAGE':lev}):
                ids=[paper.record(dict(item,symbol=f'COIN{n}USDT'),path) for n in range(count)]
                self.assertTrue(all(x is not None for x in ids))
                self.assertIsNone(paper.record(dict(item,symbol='NEXTUSDT'),path))
                with paper.session(path) as db:self.assertEqual(virtual.used(db),count*per_episode)
                virtual.bootstrap(path)
            # Existing positions retain entry leverage after environment changes.
            with patch.dict('os.environ',{'PAPER_FUTURES_LEVERAGE':'10'}):
                virtual.bootstrap(path)
                with paper.session(path) as db:self.assertEqual(virtual.used(db),count*per_episode)
            with paper.session(path) as db:
                db.execute("UPDATE virtual_state SET state='closed' WHERE episode_id=?",(ids[0],))
                self.assertEqual(virtual.used(db),(count-1)*per_episode)
            with patch.dict('os.environ',{'PAPER_FUTURES_LEVERAGE':lev}):
                self.assertIsNotNone(paper.record(dict(item,symbol='NEXTUSDT'),path))

    def test_no_fixed_five_episode_limit_for_actual_smaller_notionals(self):
        path=Path(self.tmp.name)/'six.sqlite'
        item=dict(self.item,spot_cost=D(40),future_notional=D(40))
        with patch.dict('os.environ',{'PAPER_FUTURES_LEVERAGE':'25'}):
            for n in range(6):self.assertIsNotNone(paper.record(dict(item,symbol=f'SMALL{n}USDT'),path))
            with paper.session(path) as db:self.assertEqual(virtual.used(db),D('249.6'))
            self.assertIsNone(paper.record(dict(item,symbol='SEVENTHUSDT'),path))

    def test_default_one_explicit_log_invalid_leverage_and_legacy_migration(self):
        with patch.dict('os.environ',{},clear=True),patch.object(virtual,'_leverage_logged',None),self.assertLogs(level='INFO') as logs:
            self.assertEqual(virtual.leverage(),1)
        self.assertIn('default',' '.join(logs.output))
        for invalid in ('0','-1','NaN','Infinity','unknown',''):
            with patch.dict('os.environ',{'PAPER_FUTURES_LEVERAGE':invalid}):
                with self.assertRaises(Exception):virtual.leverage()
        with paper.session(self.path) as db:
            db.execute("DELETE FROM virtual_meta WHERE name=?",(f'capital_leverage:{self.ident}',))
            db.execute("UPDATE virtual_state SET used_capital='50'")
        with patch.dict('os.environ',{'PAPER_FUTURES_LEVERAGE':'1'}):virtual.bootstrap(self.path)
        self.assertEqual(D(self.state()['used_capital']),D('100.9'))

    def test_checkpoint_full_exit_snapshot_no_telegram_or_capital_change(self):
        e=virtual.load(self.ident,self.path)
        with patch('time.time',return_value=e['started_at']+60):
            paper.poll(self.api,self.path,at=e['started_at']+60)
        with paper.session(self.path) as db:
            c=db.execute('SELECT * FROM virtual_checkpoint_details').fetchone()
            q=json.loads(c['snapshot_json'])
            self.assertEqual(c['horizon_min'],1)
            self.assertEqual(q['spot_bid'],'10')
            self.assertEqual(q['futures_ask'],'10.11')
            self.assertEqual(D(q['spot_exit']),D('10'))
            self.assertEqual(D(q['future_exit']),D('10.11'))
            self.assertEqual(D(q['spot_pnl'])+D(q['futures_pnl']),D(q['net_pnl']))
            self.assertIn('slippage',q);self.assertIn('total_trading_fees',q)
            self.assertEqual(q['funding_rate_current'],'0.0001')
            self.assertEqual(D(q['spread_change_pp']),D(q['spread'])-D('1.8'))
            self.assertEqual(virtual.used(db),D('100.9'))
        self.api.telegram.assert_not_called()
        self.assertEqual(self.state()['state'],'open')

    def test_missed_checkpoint_not_backfilled_and_unknown_funding_not_zero(self):
        e=virtual.load(self.ident,self.path)
        paper.poll(self.api,self.path,at=e['started_at']+121)
        with paper.session(self.path) as db:
            self.assertEqual(db.execute('SELECT status FROM checkpoints WHERE horizon_min=1').fetchone()[0],'missing')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM virtual_checkpoint_details').fetchone()[0],0)
        self.funding.side_effect=ValueError('missing funding')
        q=paper.lifecycle_sample(self.api,e)['pnl_snapshot']
        self.assertIsNone(q['funding_rate_current'])

    def test_statistics_mature_cohorts_actual_convergence_and_missing_history(self):
        e=virtual.load(self.ident,self.path);start=e['started_at']
        second=paper.record(dict(self.item,symbol='OTHERUSDT'),self.path)
        with paper.session(self.path) as db:
            db.execute('UPDATE virtual_metrics SET convergence_seconds=90 WHERE episode_id=?',(self.ident,))
            db.execute("UPDATE virtual_state SET state='closed' WHERE episode_id=?",(self.ident,))
            for minute in paper.HORIZONS+paper.EXTENDED_HORIZONS:
                db.execute('INSERT INTO virtual_checkpoint_details VALUES (?,?,?,?)',(second,minute,start+minute*60,'{}'))
            early=paper.lifecycle_statistics(db,at=start+30)
            self.assertIsNone(early['closed_by_pct']['1'])
            stats=paper.lifecycle_statistics(db,at=start+86401)
            self.assertEqual(stats['closed_by_pct']['1'],0)
            self.assertEqual(stats['closed_by_pct']['5'],50)
            self.assertEqual(stats['closed_by_pct']['1440'],50)
            self.assertEqual(stats['not_closed_24h_pct'],50)
            self.assertEqual(stats['median_close_min'],1.5)
            db.execute('DELETE FROM virtual_checkpoint_details WHERE episode_id=? AND horizon_min=1',(second,))
            self.assertEqual(paper.lifecycle_statistics(db,at=start+86401)['eligible_by_horizon']['1440'],1)
            db.execute('UPDATE virtual_metrics SET tracking_started_at=?',(start+1000,))
            self.assertEqual(paper.lifecycle_statistics(db,at=start+86401)['tracked'],0)

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
        self.assertIn('До funding осталось',text)
        self.assertIn('Net P&L сейчас: недоступен',text)
        self.assertIn('ожидаемый funding: +0.005050 USDT',text)
        self.assertIn('Решение принимает пользователь',text)
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

    def test_funding_warning_net_pnl_and_percent(self):
        self.funding.return_value=(D('.0001'),time.time()+1620)
        virtual.funding_warnings(self.api,self.path)
        text=self.api.telegram.call_args.args[0]
        self.assertIn('Net P&L сейчас: +0.1986 USDT (+0.1968%',text)
        self.assertIn('виртуального капитала episode',text)

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

    def test_leg_pnl_fees_slippage_saved_without_double_count(self):
        q=virtual.quote(self.api,virtual.load(self.ident,self.path))
        self.assertEqual(D(q['spot_pnl'])+D(q['futures_pnl']),D(q['trading_net']))
        self.assertEqual(D(q['spot_pnl']),D('-.05'))
        self.assertEqual(D(q['futures_pnl']),D('.24855'))
        self.assertIn('spot_entry',q['fee_breakdown'])
        self.assertIsNone(q['slippage']['spot_entry_usdt']) # Missing old input stays unknown.
        self.assertEqual(q['funding_realized_usdt'],'0') # No settlement crossed, not an assumed payment.
        self.assertEqual(D(q['slippage']['spot_exit_usdt']),0)

    def test_metrics_peak_min_convergence_and_restart_persist(self):
        for spread in ('12','10','0.08'):
            with patch.object(virtual,'quote',return_value=self.quote(spread)):
                virtual.observe(self.api,self.path)
        virtual.bootstrap(self.path)
        with paper.session(self.path) as db:
            metric=dict(db.execute('SELECT * FROM virtual_metrics WHERE episode_id=?',(self.ident,)).fetchone())
        self.assertEqual(D(metric['max_spread']),12)
        self.assertEqual(D(metric['min_spread']),D('.08'))
        self.assertEqual(D(metric['max_expansion_pp']),D('10.2'))
        self.assertGreaterEqual(metric['convergence_seconds'],0)
        self.assertEqual(json.loads(metric['latest_json'])['spread'],'0.08')

    def test_extended_checkpoints_are_statistics_only_no_timed_alerts(self):
        with paper.session(self.path) as db:
            minutes=[r['horizon_min'] for r in db.execute('SELECT horizon_min FROM checkpoints ORDER BY horizon_min').fetchall()]
        self.assertEqual(minutes,[1,5,15,30,60,180,360,720,1440])
        with patch.object(paper,'executable_sample',return_value={'spot_ask':D(10),'spot_vwap':D(10),'futures_bid':D('10.1'),'futures_vwap':D('10.1'),'raw_spread_pct':D(1),'executable_spread_pct':D(1)}):
            e=virtual.load(self.ident,self.path)
            paper.poll(self.api,self.path,e['started_at']+180*60)
        self.api.telegram.assert_not_called()
        with paper.session(self.path) as db:
            self.assertEqual(db.execute('SELECT status FROM checkpoints WHERE horizon_min=180').fetchone()[0],'observed')
        self.assertEqual(self.state()['state'],'open')

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
