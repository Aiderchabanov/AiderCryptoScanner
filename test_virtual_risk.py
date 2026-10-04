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
        self.observe(spread='2.7');self.observe(spread='2.8')
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

    def test_uncertain_send_is_not_duplicated(self):
        self.api.telegram.side_effect=RuntimeError('delivery uncertain')
        self.observe(net='-.20');virtual.bootstrap(self.path);self.observe(net='-.25')
        self.assertEqual(self.api.telegram.call_count,1)


if __name__=='__main__':unittest.main()
