import concurrent.futures
import hashlib
import hmac
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode
import requests
import app
import basis
import binance_io


class SharedBinanceTests(unittest.TestCase):
    def setUp(self):
        self.stack = __import__('contextlib').ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(app.api_blocked_until, {}, clear=True))
        self.stack.enter_context(patch.dict(binance_io.snapshots, {}, clear=True))
        self.stack.enter_context(patch.object(app.paper, 'load_api_backoff', return_value=0))
        self.save = self.stack.enter_context(patch.object(app.paper, 'save_api_backoff'))
        self.stack.enter_context(patch.object(binance_io, 'next_request', 0))
        self.stack.enter_context(patch.object(binance_io, 'last_clock', 0))

    def response(self, status=200, data=None, retry=''):
        r = Mock(status_code=status, headers={'Retry-After': retry})
        r.json.return_value = data if data is not None else {'asks': [['1','100']], 'bids': [['1','100']]}
        if status >= 400:
            r.raise_for_status.side_effect = requests.HTTPError(response=r)
        return r

    def test_spot_restart_restores_ban_without_http_even_with_cached_book(self):
        with patch.object(app.paper, 'load_api_backoff', return_value=time.time()+600), patch.object(app.requests, 'get') as get:
            with self.assertRaises(RuntimeError):
                app.get_json(app.BINANCE+'/api/v3/depth', params={'symbol':'BTCUSDT','limit':100})
        get.assert_not_called()

    def test_concurrent_ban_blocks_second_thread_and_is_persisted_for_spot(self):
        def fetch():
            try: app.get_json(app.BINANCE+'/api/v3/ticker/bookTicker')
            except (requests.HTTPError, RuntimeError): pass
        with patch.object(app.requests, 'get', return_value=self.response(418, retry='7200')) as get:
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                list(pool.map(lambda _: fetch(), range(2)))
        self.assertEqual(get.call_count,1)
        self.assertEqual(self.save.call_args.args[0], 'api.binance.com')
        self.assertGreater(self.save.call_args.args[1], time.time()+7190)

    def test_same_fresh_book_reused_but_entry_recheck_fetches_independently(self):
        url=app.BINANCE+'/api/v3/depth';params={'symbol':'BTCUSDT','limit':100}
        with patch.object(app.requests, 'get', return_value=self.response()) as get:
            one=app.get_json(url,params=params)
            one['asks'].clear()
            two=app.get_json(url,params=params)
            self.assertTrue(two['asks'])
            self.assertEqual(get.call_count,1)
            @binance_io.fresh
            def independent(): return app.get_json(url,params=params)
            independent()
            self.assertEqual(get.call_count,2)

    def test_old_snapshot_not_reused(self):
        k=binance_io.key('api.binance.com','/api/v3/depth',{'symbol':'BTCUSDT'})
        binance_io.snapshots[k]=(time.monotonic()-1,{'old':True})
        with patch.object(app.requests, 'get', return_value=self.response()) as get:
            app.get_json(app.BINANCE+'/api/v3/depth',params={'symbol':'BTCUSDT'})
        get.assert_called_once()

    def test_shared_pacing_and_weights(self):
        with patch.object(binance_io.time,'monotonic',return_value=100), patch.object(binance_io.time,'sleep') as sleep:
            binance_io.pace('api.binance.com','/api/v3/depth',{'limit':100})
            binance_io.pace('fapi.binance.com','/fapi/v1/depth',{'limit':50})
        sleep.assert_called_once_with(0.25)
        self.assertEqual(binance_io.weight('/api/v3/depth',{'limit':100}),5)
        self.assertEqual(binance_io.weight('/fapi/v1/depth',{'limit':50}),2)

    def test_signed_request_resigned_at_dispatch_without_extra_http(self):
        with patch.object(app,'BINANCE_SECRET','test-secret'), patch.object(app.time,'time',return_value=1234), patch.object(app.requests,'get',return_value=self.response(data={})) as get:
            app.get_json('https://fapi.binance.com/fapi/v1/commissionRate', params={'symbol':'BTCUSDT','timestamp':1,'recvWindow':5000,'signature':'old'},headers={'X-MBX-APIKEY':'test-key'})
        params=get.call_args.kwargs['params'].copy(); signature=params.pop('signature')
        self.assertEqual(params['timestamp'],1234000)
        self.assertEqual(signature,hmac.new(b'test-secret',urlencode(params).encode(),hashlib.sha256).hexdigest())
        get.assert_called_once()

    def test_http_date_retry_after(self):
        with patch.object(binance_io.time,'time',return_value=0):
            self.assertEqual(binance_io.retry_seconds('Thu, 01 Jan 1970 02:00:00 GMT'),7200)
        self.assertEqual(binance_io.retry_seconds('inf'),0)

    def test_depth_limits_reduced_without_changing_gate(self):
        with patch.object(app,'binance',return_value={'asks':[],'bids':[]}) as get:
            app.orderbook('Binance','BTCUSDT')
        self.assertEqual(get.call_args.args[1]['limit'],100)
        api=Mock();api.get_json.return_value={'asks':[],'bids':[]}
        basis.futures_book(api,'Binance','BTCUSDT',1)
        self.assertEqual(api.get_json.call_args.kwargs['params']['limit'],50)

    def test_existing_monitor_ban_restored_from_storage(self):
        import tempfile
        import os
        import paper
        import risk_monitor
        with tempfile.TemporaryDirectory() as directory:
            path=directory+'/paper.sqlite'
            with patch.dict(os.environ,{'PAPER_DATABASE_PATH':path,'PAPER_DATABASE_URL':'','DATABASE_URL':''}):
                with paper.session(path) as db:
                    db.execute("CREATE TABLE api_backoff (host TEXT PRIMARY KEY,until_at REAL)")
                    risk_monitor.ensure(db)
                    db.execute('INSERT INTO risk_hosts (host,until_at,status) VALUES (?,?,?)',('api.binance.com',time.time()+600,'HTTP 418'))
                with patch.object(paper,'session',side_effect=lambda: paper_session(path)):
                    self.assertGreater(original_load('api.binance.com'),time.time()+590)

    def test_parallel_different_hosts_do_not_overlap(self):
        import threading
        active=0; peak=0
        guard=threading.Lock()
        def network(*args,**kwargs):
            nonlocal active,peak
            with guard:
                active+=1;peak=max(peak,active)
            time.sleep(0.01)
            with guard: active-=1
            return self.response(data=[])
        with patch.object(app.requests,'get',side_effect=network):
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                list(pool.map(app.get_json,[app.BINANCE+'/api/v3/ticker/bookTicker','https://fapi.binance.com/fapi/v1/ticker/bookTicker']))
        self.assertEqual(peak,1)


import paper
original_load = paper.load_api_backoff
paper_session = paper.session
