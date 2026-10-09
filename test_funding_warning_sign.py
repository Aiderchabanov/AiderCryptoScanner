"""Standalone funding alerts disabled; temporary SQLite and mocked Telegram."""
import json,time,unittest
from decimal import Decimal as D
import paper,virtual,test_virtual

class FundingWarningsDisabledTests(unittest.TestCase):
    setUp=test_virtual.VirtualTests.setUp
    state=test_virtual.VirtualTests.state

    def test_all_rates_windows_and_restart_never_notify(self):
        for rate in (D('.001'),D('-.001'),D(0),None,D('NaN')):
            for offset in (1700,18000,-1):
                self.funding.return_value=(rate,time.time()+offset)
                virtual.funding_warnings(self.api,self.path)
        virtual.bootstrap(self.path)
        self.funding.return_value=(D('-.001'),time.time()+1700)
        virtual.funding_warnings(self.api,self.path)
        self.api.telegram.assert_not_called()
        with paper.session(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM virtual_events WHERE event_key LIKE 'funding:%'").fetchone()[0],0)

    def test_failure_never_notifies(self):
        self.funding.side_effect=ValueError('funding unavailable')
        virtual.funding_warnings(self.api,self.path)
        self.api.telegram.assert_not_called()
        self.assertEqual(self.state()['state'],'open')

    def test_risk_context_preserved(self):
        self.funding.return_value=(D('-.001'),time.time()+1700)
        virtual.funding_warnings(self.api,self.path)
        with paper.session(self.path) as db:
            live=json.loads(db.execute('SELECT value FROM virtual_meta WHERE name=?',(f'funding_live:{self.ident}',)).fetchone()[0])
        self.assertEqual(live['rate'],'-0.001')
        self.api.telegram.assert_not_called()

    def test_pending_funding_cancelled_other_alerts_sent_once(self):
        with paper.session(self.path) as db:
            for key in ('funding:old:negative','funding:old:positive','funding:old:scheduled','entry:test','move:test','close:test'):
                virtual.event(db,key,self.ident,key)
        virtual.dispatch(self.api,self.path)
        virtual.bootstrap(self.path)
        virtual.dispatch(self.api,self.path)
        self.assertEqual([c.args[0] for c in self.api.telegram.call_args_list],['close:test','entry:test','move:test'])
        with paper.session(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM virtual_events WHERE event_key LIKE 'funding:%' AND state='cancelled'").fetchone()[0],3)

    def test_sent_claimed_funding_history_not_replayed_or_changed(self):
        with paper.session(self.path) as db:
            for key,state in [('funding:sent','sent'),('funding:uncertain','claimed')]:
                virtual.event(db,key,self.ident,key)
                db.execute('UPDATE virtual_events SET state=? WHERE event_key=?',(state,key))
        virtual.dispatch(self.api,self.path)
        self.api.telegram.assert_not_called()
        with paper.session(self.path) as db:
            self.assertEqual(dict(db.execute('SELECT event_key,state FROM virtual_events').fetchall()),{'funding:sent':'sent','funding:uncertain':'claimed'})

    def test_admission_accounting_and_episode_unchanged(self):
        import entry_policy
        with paper.session(self.path) as db:
            before=dict(db.execute('SELECT * FROM episodes WHERE id=?',(self.ident,)).fetchone())
        accounting=virtual.accounting(self.path)
        self.funding.return_value=(D('-.001'),time.time()+1700)
        virtual.funding_warnings(self.api,self.path)
        with paper.session(self.path) as db:
            self.assertEqual(dict(db.execute('SELECT * FROM episodes WHERE id=?',(self.ident,)).fetchone()),before)
        self.assertEqual(virtual.accounting(self.path),accounting)
        self.assertIsNone(entry_policy.reason(self.item))
        self.assertEqual(entry_policy.reason(dict(self.item,funding=D('-.001'))),'REJECTED_NEGATIVE_FUNDING')
        self.api.telegram.assert_not_called()
