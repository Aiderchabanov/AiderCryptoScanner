import json
import os
import time
import unittest
from decimal import Decimal as D
from unittest.mock import MagicMock, patch

import paper
import paper_funding_model as model
import virtual
import test_virtual


class ModelTests(unittest.TestCase):
    setUp = test_virtual.VirtualTests.setUp
    state = test_virtual.VirtualTests.state

    def activate_fixture(self, exchange='BingX'):
        with paper.session(self.path) as db:
            e = dict(db.execute('SELECT * FROM episodes WHERE id=?', (self.ident,)).fetchone())
            e.update(futures_exchange=exchange, next_funding_at=120, started_at=60)
            snap = json.loads(e['cost_snapshot_json'])
            snap['paper_funding_model'] = dict(mode='ESTIMATED', method=model.METHOD, activated_at=60)
            e['cost_snapshot_json'] = json.dumps(snap)
            e['episode_id'] = self.ident; e['state'] = 'open'
        self.e = e
        self.api.basis = MagicMock()
        self.api.basis.fresh_funding.return_value = (D('.001'), 480)
        return e

    def history_fixture(self, stamps=(120,), rates=('.001',)):
        return [dict(symbol=self.e['symbol'], settlement_at=t, rate=r,
                     source=model.RATE_SOURCES[self.e['futures_exchange']]) for t,r in zip(stamps,rates)]

    def calculate(self, records=None, now=181, complete=True, price='10'):
        records = self.history_fixture() if records is None else records
        with patch.object(model, 'history', return_value=(records,complete,now)), patch.object(model, 'model_price', return_value=(price,model.PRICE_SOURCES[self.e['futures_exchange']],60)):
            return model.snapshot(self.api,self.e,self.path,now)

    def test_before_first_settlement_no_fictitious_receipt(self):
        self.activate_fixture()
        q = model.snapshot(self.api,self.e,self.path,119)
        self.assertEqual(q['model_funding_pnl_usdt'], '0')
        self.assertIsNone(q['funding_realized_usdt'])
        with paper.session(self.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM paper_funding_model_events').fetchone()[0],0)

    def test_positive_negative_zero_rates(self):
        for rate in ('.001','-.001','0'):
            with self.subTest(rate=rate):
                self.activate_fixture()
                with paper.session(self.path) as db: db.execute('DELETE FROM paper_funding_model_events')
                q = self.calculate(self.history_fixture(rates=(rate,)))
                self.assertEqual(D(q['model_funding_pnl_usdt']),D(self.e['quantity'])*10*D(rate))
                self.assertEqual(q['funding_status'],'ESTIMATED')
                self.assertIsNone(q['funding_realized_usdt'])

    def test_multiple_settlements_restart_dedup(self):
        self.activate_fixture(); records = self.history_fixture((120,240,360),('.001','-.002','.003'))
        first = self.calculate(records,361)
        with patch.object(model, 'model_price', side_effect=AssertionError('must reuse persisted price')):
            with patch.object(model,'history',return_value=(records,True,362)):
                second = model.snapshot(self.api,self.e,self.path,362)
        self.assertEqual(first['model_funding_pnl_usdt'],second['model_funding_pnl_usdt'])
        self.assertEqual(D(first['model_funding_pnl_usdt']),D(self.e['quantity'])*D('.02'))
        with paper.session(self.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM paper_funding_model_events').fetchone()[0],3)
            row=json.loads(db.execute('SELECT payload_json FROM paper_funding_model_events LIMIT 1').fetchone()[0])
            for field in ('episode_id','exchange','symbol','settlement_at','rate','quantity','source','received_at','model_price','model_price_source','model_amount_usdt','status','method'):
                self.assertIn(field,row)

    def test_missing_historical_rate_unknown_not_zero(self):
        self.activate_fixture()
        q=self.calculate([])
        self.assertIsNone(q['model_funding_pnl_usdt'])
        with paper.session(self.path) as db:
            self.assertEqual(db.execute('SELECT status FROM paper_funding_model_events').fetchone()[0],'UNKNOWN')

    def test_missing_model_price_unknown_then_recovery(self):
        self.activate_fixture(); records=self.history_fixture()
        with patch.object(model,'history',return_value=(records,True,181)), patch.object(model,'model_price',side_effect=ValueError):
            q=model.snapshot(self.api,self.e,self.path,181)
        self.assertIsNone(q['model_funding_pnl_usdt'])
        self.assertEqual(self.calculate(now=182)['funding_status'],'ESTIMATED')
        with paper.session(self.path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM paper_funding_model_events').fetchone()[0],1)

    def test_missing_prior_checkpoint_and_duplicate_history_block(self):
        self.activate_fixture(); self.calculate()
        for records, complete in ((self.history_fixture((240,),('.001',)),True), (self.history_fixture()*2,True), (self.history_fixture(),False)):
            self.assertIsNone(self.calculate(records,241,complete)['model_funding_pnl_usdt'])

    def test_history_rate_changed_cannot_revalue_receipt(self):
        self.activate_fixture();self.calculate()
        q=self.calculate(self.history_fixture(rates=('.002',)),182)
        self.assertIsNone(q['model_funding_pnl_usdt'])

    def test_old_episode_ignores_global_activation(self):
        e=virtual.load(self.ident,self.path)
        before=json.dumps(e,sort_keys=True,default=str)
        with patch.dict(os.environ,{'PAPER_FUNDING_MODE':'ESTIMATED'}):
            self.assertFalse(model.enabled(e))
            e['next_funding_at']=time.time()-1
            q=virtual.quote(self.api,e,self.path)
        self.assertEqual(q['funding_status'],'UNKNOWN_SETTLEMENT_PNL')
        self.assertIsNone(q['net_pnl'])
        self.assertNotIn('paper_funding_model',json.loads(virtual.load(self.ident,self.path)['cost_snapshot_json']))

    def test_new_episode_activation_is_persisted_and_not_env_dependent(self):
        item=dict(self.item,symbol='XRPUSDT')
        with patch.dict(os.environ,{'PAPER_FUNDING_MODE':'ESTIMATED'}):
            ident=paper.record(item,self.path)
        self.assertIsNotNone(ident)
        e=virtual.load(ident,self.path)
        with patch.dict(os.environ,{'PAPER_FUNDING_MODE':'CONFIRMED'}): self.assertTrue(model.enabled(e))

    def test_close_eligibility_known_nonnegative_and_spread_only(self):
        e=self.activate_fixture()
        for net,status,allowed in (('1','ESTIMATED',True),('-1','ESTIMATED',False),(None,'UNKNOWN',False)):
            for spread in ('.10','-.10','.11'):
                q=dict(funding_mode='ESTIMATED',funding_status=status,model_net_pnl_usdt=net,
                       model_funding_pnl_usdt='0',spot_pnl=net,futures_pnl='0',model_valid_until=time.time()+100,spread=spread)
                value=model.closing_net(e,q)
                can_close=value is not None and value>=0 and abs(D(spread))<=paper.CLOSED_PCT
                self.assertEqual(can_close,allowed and abs(D(spread))<=D('.10'))
        q['model_valid_until']=time.time()-1
        self.assertIsNone(model.closing_net(e,q))

    def test_quote_model_total_fees_once_and_clear_telegram(self):
        import basis
        self.api.basis=basis
        e=virtual.load(self.ident,self.path)
        original=virtual.quote(self.api,e,self.path)
        snap=json.loads(e['cost_snapshot_json']);snap['paper_funding_model']=dict(mode='ESTIMATED',method=model.METHOD,activated_at=e['started_at'])
        e['cost_snapshot_json']=json.dumps(snap)
        estimated=dict(funding_status='ESTIMATED',funding_realized_usdt=None,model_funding_pnl_usdt='-.2',funding_unknown_reason=None,funding_settlements_seen=1,model_valid_until=time.time()+100)
        with patch.object(model,'snapshot',return_value=estimated): q=virtual.quote(self.api,e,self.path)
        self.assertEqual(D(q['model_net_pnl_usdt']),D(original['trading_net'])-D('.2'))
        self.assertEqual(q['total_trading_fees'],original['total_trading_fees'])
        self.assertIsNone(q['net_pnl']);self.assertIsNone(virtual.realized_result(q))
        self.assertIn('Funding: ESTIMATED',virtual.pnl_line(q))
        self.assertIn('Не подтверждённое начисление биржи',virtual.pnl_line(q))

    def test_exact_previous_candle_all_providers_no_current_price(self):
        api=MagicMock()
        api.bingx.request.return_value=[dict(openTime=60000,closeTime=120000,close='2'),dict(openTime=120000,closeTime=180000,close='999')]
        api.mexc.request.return_value={'time':[60,120],'close':['2','999']}
        api.get_json.return_value=[[60000,0,0,0,'2',0,119999]]
        api.gate.return_value=[{'t':60,'c':'2'},{'t':120,'c':'999'}]
        for venue in ('BingX','MEXC','Binance','Gate'):
            self.assertEqual(model.model_price(api,venue,'BTCUSDT',120)[0],'2')
        api.bingx.request.return_value=[]
        with self.assertRaises(ValueError):model.model_price(api,'BingX','BTCUSDT',120)

    def test_actual_auto_close_path_mode_net_and_unknown(self):
        e=virtual.load(self.ident,self.path)
        snap=json.loads(e['cost_snapshot_json']); snap['paper_funding_model']=dict(mode='ESTIMATED',method=model.METHOD,activated_at=e['started_at'])
        with paper.session(self.path) as db:db.execute('UPDATE episodes SET cost_snapshot_json=? WHERE id=?',(json.dumps(snap),self.ident))
        for net, status in (('-1','ESTIMATED'),(None,'UNKNOWN'),('1','ESTIMATED')):
            q=test_virtual.VirtualTests.quote(self,'.08')
            q.update(funding_mode='ESTIMATED',funding_status=status,net_pnl=None,
                     model_net_pnl_usdt=net,model_funding_pnl_usdt='0',spot_pnl=net,
                     futures_pnl='0',funding_realized_usdt=None,total_trading_fees='.1',model_valid_until=time.time()+100)
            with patch.object(virtual,'quote',return_value=q):virtual.observe(self.api,self.path)
            self.assertEqual(self.state()['state'],'closed' if net=='1' else 'open')
        closed=json.loads(self.state()['close_json'])
        self.assertEqual(closed['model_realized_net_pnl_usdt'],'1')
        self.assertIsNone(closed['realized_net_pnl_usdt'])
        self.assertIsNone(closed['funding_realized_usdt'])

    def test_model_closed_result_not_labelled_confirmed_balance(self):
        self.test_actual_auto_close_path_mode_net_and_unknown()
        a=virtual.accounting(self.path)
        self.assertEqual(a['confirmed_realized_pnl'],0)

    def test_new_funding_event_after_restart_not_omitted(self):
        self.activate_fixture();self.calculate()
        self.api.basis.fresh_funding.return_value=(D('.001'),600)
        # 480 was persisted as expected during previous check. Its omission
        # must block closure, even if a provider claims the list is complete.
        q=self.calculate(self.history_fixture((120,240,360),('.001','.001','.001')),481)
        self.assertIsNone(q['model_funding_pnl_usdt'])
        q=self.calculate(self.history_fixture((120,240,360,480),('.001','.001','.001','.001')),482)
        self.assertEqual(q['funding_settlements_seen'],4)

    def test_all_public_transport_paths_get_only_no_credentials(self):
        import bingx,mexc
        for module,venue,params in ((bingx,'BingX',dict(symbol='BTC-USDT',interval='1m',startTime=60000,endTime=120000,limit=3)),
                                    (mexc,'MEXC',dict(symbol='BTC_USDT',interval='Min1',start=60,end=120))):
            client=module.Client();client.enabled=True;client.key='fixture-key';client.secret='fixture-secret'
            response=MagicMock(status_code=200);response.json.return_value={'success':True,'code':0,'data':[]}
            with patch.object(module.requests,'get',return_value=response) as request:client.request('funding_model_candles',params)
            self.assertNotIn('fixture-key',str(request.call_args.kwargs))
            self.assertNotIn('signature',request.call_args.kwargs['params'])
            self.assertFalse(request.call_args.kwargs['allow_redirects'])

    def test_real_history_millisecond_precision_not_false_unknown(self):
        self.activate_fixture(); now=181.123456
        with patch.object(model,'history',return_value=(self.history_fixture(),True,181.123)), patch.object(model,'model_price',return_value=('10','fixture-official-source',60)):
            self.assertEqual(model.snapshot(self.api,self.e,self.path,now)['funding_status'],'ESTIMATED')

    def test_repeated_unknown_does_not_grow_audit_payload(self):
        self.activate_fixture(); records=self.history_fixture()
        with patch.object(model,'history',return_value=(records,True,181)),patch.object(model,'model_price',side_effect=ValueError):
            model.snapshot(self.api,self.e,self.path,181)
            with paper.session(self.path) as db: before=db.execute('SELECT payload_json FROM paper_funding_model_events').fetchone()[0]
            model.snapshot(self.api,self.e,self.path,181)
            with paper.session(self.path) as db: after=db.execute('SELECT payload_json FROM paper_funding_model_events').fetchone()[0]
        self.assertEqual(before,after)


if __name__=='__main__':unittest.main()
