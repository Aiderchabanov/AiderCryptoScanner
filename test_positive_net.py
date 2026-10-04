import json,unittest
from decimal import Decimal as D
from unittest.mock import patch
import entry_policy,paper,virtual,test_virtual

class PositiveNetTests(unittest.TestCase):
    setUp=test_virtual.VirtualTests.setUp
    quote=test_virtual.VirtualTests.quote
    state=test_virtual.VirtualTests.state

    def test_threshold_funding_and_rejection_counters(self):
        for value,expected in (('-1','REJECTED_NON_POSITIVE_NET_ENTRY'),('0','REJECTED_NON_POSITIVE_NET_ENTRY'),('.19','REJECTED_LOW_EXPECTED_NET_RETURN'),('.20',None)):
            item=dict(self.item,pct=D(value))
            self.assertEqual(entry_policy.reason(item),expected)
            if expected:
                self.assertIsNone(paper.record(item,self.path))
                with paper.session(self.path) as db:
                    self.assertIsNotNone(db.execute('SELECT value FROM virtual_meta WHERE name=?',('entry_rejection:'+expected,)).fetchone())
        for funding in (None,D('NaN'),D(0),D('-.01')):
            item=dict(self.item,funding=funding,pct=D(1))
            self.assertIsNotNone(entry_policy.reason(item))
            self.assertIsNone(paper.record(item,self.path))

    def test_new_close_requires_confirmed_nonnegative_net_legacy_preserved(self):
        for policy in (False,True):
            for net in ('-.15','0','.15',None):
                with self.subTest(policy=policy,net=net):
                    with paper.session(self.path) as db:
                        snap=json.loads(db.execute('SELECT cost_snapshot_json FROM episodes WHERE id=?',(self.ident,)).fetchone()['cost_snapshot_json'])
                        if policy:snap['entry_policy']=entry_policy.VERSION
                        else:snap.pop('entry_policy',None)
                        db.execute('UPDATE episodes SET cost_snapshot_json=? WHERE id=?',(json.dumps(snap),self.ident))
                        db.execute("UPDATE virtual_state SET state='open' WHERE episode_id=?",(self.ident,))
                    q=self.quote('-.08');q['net_pnl']=net
                    with patch.object(virtual,'quote',return_value=q):virtual.observe(self.api,self.path)
                    self.assertEqual(self.state()['state'],'closed' if not policy or (net is not None and D(net)>=0) else 'open')
                    if policy and net is None:self.assertEqual(json.loads(self.state()['current_json'])['auto_close_blocked_reason'],'UNKNOWN_SETTLEMENT_PNL')
                    if policy and net=='-.15':self.assertEqual(json.loads(self.state()['current_json'])['auto_close_blocked_reason'],'CONVERGED_BUT_NET_NEGATIVE')
