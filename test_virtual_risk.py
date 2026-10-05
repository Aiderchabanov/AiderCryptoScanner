"""Risk monitoring tests: mocked quotes, local SQLite; no exchange HTTP."""
import json
import unittest
from unittest.mock import patch
import paper, virtual
import test_virtual


class VirtualRiskTests(unittest.TestCase):
    setUp = test_virtual.VirtualTests.setUp
    quote = test_virtual.VirtualTests.quote
    state = test_virtual.VirtualTests.state

    def observe(self, spread='1.8', net='.10'):
        q=self.quote(spread);q['net_pnl']=net;q['trading_net']=net or '-.25'
        with patch.object(virtual,'quote',return_value=q):
            virtual.observe(self.api,self.path)

    def levels(self):
        with paper.session(self.path) as db:
            return json.loads(db.execute('SELECT value FROM virtual_meta WHERE name=?',(f'risk_levels:{self.ident}',)).fetchone()['value'])

    def test_pnl_minus_020_one_alert_no_scan_spam_restart_rearm(self):
        self.observe(net='-.19');self.assertEqual(self.api.telegram.call_count,0)
        self.observe(net='-.20');self.assertEqual(self.api.telegram.call_count,1)
        self.assertIn('<= -0.20 USDT',self.api.telegram.call_args.args[0])
        self.observe(net='-.25');virtual.bootstrap(self.path);self.observe(net='-.25')
        self.assertEqual(self.api.telegram.call_count,1)
        self.assertEqual(self.state()['state'],'open')
        self.observe(net='-.19');self.observe(net='-.20')
        self.assertEqual(self.api.telegram.call_count,2)

    def test_spread_plus_1pp_one_alert_restart_recovery_recross(self):
        self.observe(spread='2.8');self.assertEqual(self.api.telegram.call_count,1)
        body=self.api.telegram.call_args.args[0]
        self.assertIn('SPREAD EXPANDED: +1.0 p.p.',body)
        self.assertIn('Expansion: +1.0000 p.p.',body)
        self.assertIn('Funding: +0.0100%',body)
        self.assertIn('Time to next funding:',body)
        self.observe(spread='2.9');virtual.bootstrap(self.path);self.observe(spread='2.9')
        self.assertEqual(self.api.telegram.call_count,1)
        self.observe(spread='2.2');self.observe(spread='2.8') # Recovery below +0.5 p.p.
        self.assertEqual(self.api.telegram.call_count,2)
        self.assertEqual(len(virtual.rows(self.path)),1) # No additional entry.

    def test_independent_pnl_levels_partial_recovery_and_repeated_crossing(self):
        self.observe(net='-.20');self.observe(net='-.50');self.observe(net='-1.00')
        self.assertEqual(self.api.telegram.call_count,3)
        self.observe(net='-.80');self.observe(net='-1.00')
        self.assertEqual(self.api.telegram.call_count,4)
        self.assertEqual(self.levels()['pnl_warning_level'],3)

    def test_unknown_pnl_never_zero_or_recovery(self):
        self.observe(net='-.25');self.observe(net=None);self.observe(net='-.25')
        self.assertEqual(self.api.telegram.call_count,1)
        self.observe(spread='2.8',net=None)
        self.assertIn('Current Net P&L: UNKNOWN',self.api.telegram.call_args.args[0])
        self.assertEqual(self.state()['state'],'open')

    def test_unknown_funding_label_no_extra_fetches(self):
        self.funding.return_value=(None,None)
        self.observe(spread='2.8')
        self.assertEqual(self.funding.call_count,1) # Existing funding warning refresh only.
        self.assertIn('Funding: UNKNOWN',self.api.telegram.call_args.args[0])
        self.api.orderbook.assert_not_called();self.future.assert_not_called()

    def test_negative_and_unknown_net_convergence_stays_open_positive_closes(self):
        self.observe(spread='-.0964',net='-.1622')
        self.assertEqual(self.state()['state'],'open')
        self.observe(spread='.08',net=None);self.assertEqual(self.state()['state'],'open')
        self.observe(spread='.08',net='.01');self.assertEqual(self.state()['state'],'closed')

    def test_closed_or_stale_snapshots_never_create_risk_alerts(self):
        e=virtual.load(self.ident,self.path);q=self.quote('3');q['net_pnl']='-1'
        with paper.session(self.path) as db:
            q['at']-=31;virtual.risk_alert(db,e,q)
            q['at']+=31;db.execute("UPDATE virtual_state SET state='closed'")
            virtual.risk_alert(db,e,q)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM virtual_events').fetchone()[0],0)

    def test_multiple_new_levels_grouped_in_one_risk_alert(self):
        self.observe(spread='3.8',net='-1.00')
        with paper.session(self.path) as db:
            events=db.execute("SELECT body FROM virtual_events WHERE event_key LIKE 'risk:%'").fetchall()
        self.assertEqual(len(events),1)
        self.assertIn('+1.0 p.p., +2.0 p.p.',events[0]['body'])
        self.assertIn('<= -1.00 USDT',events[0]['body'])

    def test_close_between_queue_and_dispatch_cancels_risk(self):
        e=virtual.load(self.ident,self.path);q=self.quote('2.8')
        with paper.session(self.path) as db:
            virtual.risk_alert(db,e,q)
            db.execute("UPDATE virtual_state SET state='closed'")
        virtual.dispatch(self.api,self.path)
        self.api.telegram.assert_not_called()
        with paper.session(self.path) as db:
            self.assertEqual(db.execute('SELECT state FROM virtual_events').fetchone()[0],'cancelled')

    def test_notified_minus_050_not_repeated_in_spread_only_alert(self):
        self.observe(net='-.30');self.observe(net='-.53')
        self.api.telegram.reset_mock()
        self.observe(spread='2.8',net='-.66')
        self.assertEqual(self.api.telegram.call_count,1)
        body=self.api.telegram.call_args.args[0]
        self.assertIn('SPREAD EXPANDED: +1.0 p.p.',body)
        self.assertNotIn('NET P&L crossed:',body)
        self.assertIn('Current Net P&L: -0.6600 USDT',body)

    def test_new_minus_050_and_new_spread_grouped_only_new_events(self):
        self.observe(net='-.30');self.api.telegram.reset_mock()
        self.observe(spread='2.8',net='-.53')
        self.assertEqual(self.api.telegram.call_count,1)
        body=self.api.telegram.call_args.args[0]
        self.assertIn('SPREAD EXPANDED: +1.0 p.p.',body)
        self.assertIn('NET P&L crossed: <= -0.50 USDT',body)
        self.assertNotIn('<= -0.20 USDT',body)

    def test_restart_preserves_notified_pnl_in_spread_only_alert(self):
        self.observe(net='-.53');virtual.bootstrap(self.path)
        self.api.telegram.reset_mock();self.observe(spread='2.8',net='-.66')
        self.assertEqual(self.api.telegram.call_count,1)
        self.assertNotIn('NET P&L crossed:',self.api.telegram.call_args.args[0])

    def test_legacy_notified_state_migration_no_duplicate(self):
        with paper.session(self.path) as db:
            db.execute('INSERT INTO virtual_meta (name,value) VALUES (?,?)',
                       (f'risk_levels:{self.ident}',json.dumps({'pnl_warning_level':2,'spread_expansion_level':0,'generation':1})))
        self.observe(spread='2.8',net='-.66')
        self.assertEqual(self.api.telegram.call_count,1)
        self.assertNotIn('NET P&L crossed:',self.api.telegram.call_args.args[0])
        self.assertEqual(self.levels()['pnl_notified'],['-0.20','-0.50'])

    def test_old_snapshot_does_not_rearm_threshold(self):
        self.observe(net='-.53')
        e=virtual.load(self.ident,self.path);q=self.quote('1.8');q['net_pnl']='-.30'
        q['at']=self.levels()['last_sample_at']-1
        with paper.session(self.path) as db:virtual.risk_alert(db,e,q)
        self.api.telegram.reset_mock();self.observe(spread='2.8',net='-.66')
        self.assertEqual(self.api.telegram.call_count,1)
        self.assertNotIn('NET P&L crossed:',self.api.telegram.call_args.args[0])

    def test_uncertain_send_is_not_duplicated(self):
        self.api.telegram.side_effect=RuntimeError('delivery uncertain')
        self.observe(net='-.20');virtual.bootstrap(self.path);self.observe(net='-.25')
        self.assertEqual(self.api.telegram.call_count,1)

    def risk_cycle(self, expansion):
        e=virtual.load(self.ident,self.path)
        q=self.quote(str(virtual.D(e['executable_spread_pct'])+virtual.D(expansion)))
        with paper.session(self.path) as db: virtual.risk_alert(db,e,q)
        return q

    def risk_bodies(self):
        with paper.session(self.path) as db:
            return [r['body'] for r in db.execute("SELECT body FROM virtual_events WHERE event_key LIKE 'risk:%' ORDER BY event_key").fetchall()]

    def test_hysteresis_101_099_101_no_repeat(self):
        for x in ('1.01','.99','1.01'):self.risk_cycle(x)
        self.assertEqual(len(self.risk_bodies()),1)
        self.assertEqual(self.levels()['spread_notified'],[1])

    def test_hysteresis_101_040_105_rearms(self):
        for x in ('1.01','.40','1.05'):self.risk_cycle(x)
        self.assertEqual(len(self.risk_bodies()),2)

    def test_hysteresis_210_180_205_no_repeat(self):
        for x in ('2.10','1.80','2.05'):self.risk_cycle(x)
        self.assertEqual(len(self.risk_bodies()),1)
        self.assertEqual(self.levels()['spread_notified'],[1,2])

    def test_hysteresis_210_140_205_only_level_2_rearms(self):
        for x in ('2.10','1.40','2.05'):self.risk_cycle(x)
        bodies=self.risk_bodies();self.assertEqual(len(bodies),2)
        self.assertIn('SPREAD EXPANDED: +2.0 p.p.',bodies[-1])
        self.assertNotIn('+1.0 p.p.',bodies[-1])
        self.assertNotIn('NET P&L crossed:',bodies[-1])

    def test_hysteresis_exact_boundary_restart_and_legacy_migration(self):
        with paper.session(self.path) as db:
            db.execute('INSERT INTO virtual_meta (name,value) VALUES (?,?)',
                       (f'risk_levels:{self.ident}',json.dumps({'pnl_warning_level':0,'spread_expansion_level':2,'generation':1})))
        self.risk_cycle('1.50');virtual.bootstrap(self.path);self.risk_cycle('2.05')
        self.assertEqual(len(self.risk_bodies()),0)
        self.risk_cycle('1.49');self.risk_cycle('2.05')
        self.assertEqual(len(self.risk_bodies()),1)
        self.assertNotIn('+1.0 p.p.',self.risk_bodies()[-1])

    def test_spike_snapshot_existing_quote_no_api_and_unknowns_not_zero(self):
        e=virtual.load(self.ident,self.path)
        q=virtual.quote(self.api,e) # Existing normal observation supplies the book.
        q['spread']='7.0';q['funding_status']='UNKNOWN_SETTLEMENT_PNL';q['net_pnl']=None
        self.api.reset_mock();self.future.reset_mock();self.funding.reset_mock()
        with paper.session(self.path) as db:
            virtual.risk_alert(db,e,q)
            row=db.execute("SELECT value FROM virtual_meta WHERE name LIKE 'spike_snapshot:%'").fetchone()
            data=json.loads(row['value'])
            self.assertEqual(data['spot_best_ask'],'10.01')
            self.assertEqual(data['spot_executable_avg'],q['spot_entry_vwap_now'])
            self.assertEqual(data['spot_depth_usdt'],'1001.00')
            self.assertEqual(data['futures_depth_usdt'],'1010.0')
            self.assertIsNone(data['spot_quote_age_ms'])
            self.assertEqual(data['stale_spot_quote'],'unknown')
            self.assertEqual(data['quote_time_mismatch'],'unknown')
            self.assertFalse(data['thin_spot_depth'])
            self.assertEqual(data['funding_settlement_status'],'UNKNOWN_SETTLEMENT_PNL')
            self.assertEqual(data['trading_net_pnl'],q['trading_net'])
            self.assertIsNone(data['funding'])
            virtual.risk_alert(db,e,q) # Same quote never duplicates the snapshot.
            self.assertEqual(db.execute("SELECT COUNT(*) FROM virtual_meta WHERE name LIKE 'spike_snapshot:%'").fetchone()[0],1)
        self.api.orderbook.assert_not_called();self.api.fee.assert_not_called()
        self.future.assert_not_called();self.funding.assert_not_called()

    def test_no_spike_snapshot_below_5_and_quality_flags_from_known_values(self):
        self.risk_cycle('4.99')
        with paper.session(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM virtual_meta WHERE name LIKE 'spike_snapshot:%'").fetchone()[0],0)
            e=virtual.load(self.ident,self.path);q=self.quote('6.8')
            q.update(spot_quote_age_ms=31000,futures_quote_age_ms=100,
                     quote_time_difference_ms=30900,spot_depth_quantity='1')
            virtual.risk_alert(db,e,q)
            data=json.loads(db.execute("SELECT value FROM virtual_meta WHERE name LIKE 'spike_snapshot:%'").fetchone()['value'])
            self.assertTrue(data['stale_spot_quote']);self.assertFalse(data['stale_futures_quote'])
            self.assertTrue(data['quote_time_mismatch']);self.assertTrue(data['thin_spot_depth'])
            self.assertEqual(data['thin_futures_depth'],'unknown')

    def test_continuing_spike_retains_new_snapshot_without_repeating_alert(self):
        self.risk_cycle('5.00');self.risk_cycle('5.25')
        self.assertEqual(len(self.risk_bodies()),1)
        with paper.session(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM virtual_meta WHERE name LIKE 'spike_snapshot:%'").fetchone()[0],2)


if __name__=='__main__':unittest.main()
