import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock,patch
import requests
import app,bingx,paper,risk_monitor as risk

class RiskTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'isolated.sqlite'
        self.api=MagicMock();self.monitor=risk.Monitor(self.api,self.path)
    def test_single_failure_noncritical_repeat_and_restart_once(self):
        for t in (1,2,3):
            result=self.monitor.save('BingX','Futures market',risk.HealthError('HTTP 503'),at=t)
            self.assertEqual(result['state'],'CRITICAL' if t==3 else 'DEGRADED')
        self.assertEqual(self.api.telegram.call_count,1)
        risk.Monitor(self.api,self.path).save('BingX','Futures market',risk.HealthError('HTTP 503'),at=4)
        self.assertEqual(self.api.telegram.call_count,1)
        text=self.api.telegram.call_args.args[0]
        self.assertIn('Биржа: BingX',text);self.assertIn('HTTP 503',text);self.assertIn('Повторений: 3',text)
    def test_sustained_failure_recovery_new_incident_last_success(self):
        self.monitor.save('Gate','Funding',at=100)
        self.monitor.save('Gate','Funding',risk.HealthError('TIMEOUT'),at=200)
        result=self.monitor.save('Gate','Funding',risk.HealthError('TIMEOUT'),at=501)
        self.assertEqual(result['state'],'CRITICAL');self.assertIn('1970-01-01',self.api.telegram.call_args.args[0])
        result=self.monitor.save('Gate','Funding',at=600);self.assertEqual(result['failures'],0)
        for at in (601,602,603):self.monitor.save('Gate','Funding',risk.HealthError('TIMEOUT'),at=at)
        self.assertEqual(self.api.telegram.call_count,2)
    def test_venues_and_components_independent_no_scan_calls(self):
        def request(exchange,url,params):
            if exchange=='Binance':raise risk.HealthError('HTTP 418')
            return {}
        with patch.object(self.monitor,'request',side_effect=request),patch.object(risk,'validate',return_value=True):
            first=self.monitor.once('Binance');second=self.monitor.once('Gate');third=self.monitor.once('BingX')
        self.assertTrue(all(x['state']=='DEGRADED' for x in first))
        self.assertTrue(all(x['state']=='HEALTHY' for x in second+third))
        self.api.scan_once.assert_not_called();self.api.basis.scan.assert_not_called()
    def test_multiple_alerts_name_each_exchange(self):
        for exchange in risk.EXCHANGES:
            for at in (1,2,3):self.monitor.save(exchange,'Spot market',risk.HealthError('HTTP 503'),at=at)
        texts=[call.args[0] for call in self.api.telegram.call_args_list]
        self.assertEqual(len(texts),3)
        for exchange,text in zip(risk.EXCHANGES,texts):self.assertIn('Биржа: '+exchange,text)
    def test_bingx_timeout_auth_5xx_rate_limit_classification_and_get_only(self):
        url=risk.ENDPOINTS['BingX'][0][1]
        with patch.object(risk.requests,'get',side_effect=requests.Timeout('secreturl')):
            with self.assertRaises(risk.HealthError) as ctx:self.monitor.request('BingX',url,{})
            self.assertEqual(ctx.exception.status,'TIMEOUT');self.assertNotIn('secret',str(ctx.exception))
        for status in (503,401,403,418,429):
            response=MagicMock(status_code=status,headers={'Retry-After':'120'})
            with patch.object(risk.requests,'get',return_value=response) as get,patch.object(self.monitor,'blocked'),patch.object(self.monitor,'block') as block,patch.object(risk.time,'sleep'):
                with self.assertRaises(risk.HealthError) as ctx:self.monitor.request('BingX',url,{})
                self.assertEqual(ctx.exception.status,'HTTP '+str(status));self.assertFalse(get.call_args.kwargs['allow_redirects'])
                block.assert_called()
        with self.assertRaises(risk.HealthError):self.monitor.request('BingX',bingx.BASE_URL+'/order',{})
    def test_retry_after_persistent_host_cooldown_and_no_extra_requests(self):
        url=risk.ENDPOINTS['BingX'][0][1]
        response=MagicMock(status_code=429,headers={'Retry-After':'120'})
        with patch.object(risk.requests,'get',return_value=response) as get:
            with self.assertRaises(risk.HealthError):self.monitor.request('BingX',url,{})
            restarted=risk.Monitor(self.api,self.path)
            with self.assertRaises(risk.HealthError) as ctx:restarted.request('BingX',url,{})
            self.assertIn('COOLDOWN',ctx.exception.status);self.assertEqual(get.call_count,1)
        self.assertEqual(risk.retry_seconds('Thu, 01 Jan 1970 00:02:00 GMT',now=0),120)
    def test_bingx_stale_depth_funding_missing_and_api_rate_limits(self):
        good={'code':0,'data':{'ts':int(time.time()*1000),'asks':[['10','2']],'bids':[['9','2']]}}
        self.assertTrue(risk.validate('BingX','depth',good))
        good['data']['ts']-=60000
        with self.assertRaises(risk.HealthError) as ctx:risk.validate('BingX','depth',good)
        self.assertEqual(ctx.exception.status,'STALE_MARKET_DATA')
        for data in ({'code':100410},{'code':0,'data':{'lastFundingRate':None,'nextFundingTime':time.time()*1000+3600000}}):
            with self.assertRaises(risk.HealthError):risk.validate('BingX','funding',data)
    def test_bingx_shared_limiter_does_not_depend_on_scanner_enabled(self):
        client=bingx.Client();client.enabled=False;client._next_request=time.monotonic()+2
        monitor=risk.Monitor(self.api,self.path,client)
        response=MagicMock(status_code=200);response.json.return_value={'code':0,'data':{}}
        with patch.object(risk.requests,'get',return_value=response),patch.object(risk.time,'sleep') as sleep:
            monitor.request('BingX',risk.ENDPOINTS['BingX'][0][1],{})
            self.assertGreater(sleep.call_args.args[0],1)
        self.assertFalse(client.enabled)
    def test_read_only_key_uses_only_fee_allowlist_no_secret_logs(self):
        client=MagicMock(enabled=True,credentials_ready=True)
        client.request.return_value={'takerCommissionRate':'0.001','commission':{'takerCommissionRate':'0.001'}}
        monitor=risk.Monitor(self.api,self.path,client)
        with patch.object(monitor,'request',return_value={}),patch.object(risk,'validate',return_value=True):monitor.once('BingX')
        self.assertEqual([call.args[0] for call in client.request.call_args_list],['spot_fee','futures_fee'])
    def test_each_exchange_has_spot_futures_and_funding_checks(self):
        for exchange in risk.EXCHANGES:
            names={x[0] for x in risk.ENDPOINTS[exchange]}
            self.assertEqual(names,{'Spot market','Spot depth','Futures market','Futures depth','Funding'})

if __name__=='__main__':unittest.main()
