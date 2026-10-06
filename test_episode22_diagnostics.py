import copy
import json
import unittest
from unittest.mock import patch
import episode22_diagnostics as diag


class Episode22DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.e={'episode_id':22,'symbol':'CATEUSDT','executable_spread_pct':'2',
                'next_funding_at':100}
        self.q={'at':200,'spread':'3.01','net_pnl':None,'trading_net':'-.8',
                'funding_status':'UNKNOWN_SETTLEMENT_PNL',
                'funding_realized_usdt':None,'api_key':'SECRET_SENTINEL'}

    def output(self,before=(1,),after=(1,),levels=()):
        with patch.object(diag.logging,'info') as log:
            diag.emit(self.e,self.q,'hysteresis',before,after,levels)
        return json.loads(log.call_args.args[1])

    def test_readonly_inputs_no_credentials_and_unknown_not_zero(self):
        before=[1];after=[1];original=copy.deepcopy((self.e,self.q,before,after))
        result=self.output(before,after)
        self.assertEqual((self.e,self.q,before,after),original)
        self.assertNotIn('SECRET_SENTINEL',json.dumps(result))
        self.assertEqual(result['full_net_pnl'],'UNKNOWN')
        self.assertEqual(result['funding_realized_usdt'],'UNKNOWN')
        self.assertFalse(result['plus_1_new_event_created'])

    def test_only_episode_22_cate(self):
        with patch.object(diag.logging,'info') as log:
            self.e['episode_id']=21;diag.emit(self.e,self.q,'observation')
            self.e['episode_id']=22;self.e['symbol']='OTHER';diag.emit(self.e,self.q,'observation')
        log.assert_not_called()

    def test_reset_recross_and_duplicate_trace(self):
        self.q['spread']='2.40'
        self.assertTrue(self.output((1,),(),())['plus_1_reset_triggered'])
        self.q['spread']='3.05'
        r=self.output((),(1,),(1,));self.assertTrue(r['plus_1_new_event_created'])
        self.assertFalse(r['potential_duplicate_plus_1'])
        self.assertTrue(self.output((1,),(1,),(1,))['potential_duplicate_plus_1'])

    def test_close_decision_only_reported_never_changes_quote(self):
        for spread,net,expected in (('.08','.01',True),('-.08','-.1',False),('.08',None,False),('.11','.1',False)):
            self.q.update(spread=spread,net_pnl=net)
            original=copy.deepcopy(self.q)
            self.assertEqual(self.output()['auto_close_eligible'],expected)
            self.assertEqual(self.q,original)

    def test_logging_failure_does_not_interrupt_no_external_dependencies(self):
        with patch.object(diag.logging,'info',side_effect=RuntimeError):
            diag.emit(self.e,self.q,'observation')
        with patch.object(diag.logging,'info') as log:
            self.q['spot_exit']='SECRET_SENTINEL';diag.emit(self.e,self.q,'observation')
        self.assertNotIn('SECRET_SENTINEL',log.call_args.args[1])


if __name__=='__main__':unittest.main()
