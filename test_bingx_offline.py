import json
import unittest
from unittest.mock import Mock, patch
from decimal import Decimal
import bingx, bingx_diagnostics as diag, basis, paper, virtual
from test_bingx import client, response
import test_virtual


def offline_error(message='PUMPBTC-USDT is offline currently', code=109418):
    reply=response({},200,code);reply.json.return_value['msg']=message
    with patch.object(bingx.requests,'get',return_value=reply) as get:
        try:
            client().orderbook('PUMPBTCUSDT',True)
        except bingx.BingXUnavailable as exc:
            get.assert_called_once()
            return exc
    raise AssertionError('expected rejection')


class OfflineClassificationTests(unittest.TestCase):
    def test_official_failure_classification(self):
        q=offline_error().diagnostic_context
        self.assertEqual(q['reason'],'BINGX_SYMBOL_OFFLINE')
        self.assertEqual(q['availability_status'],'OFFLINE')
        self.assertEqual((q['market'],q['symbol'],q['operation'],q['endpoint_class'],q['http_status'],q['api_code']),
                         ('Futures','PUMPBTC-USDT','orderbook','futures_depth',200,109418))

    def test_unrelated_errors_and_other_symbols_not_offline(self):
        for message in ('rate limit','service unavailable','BTC-USDT is offline currently'):
            self.assertFalse(diag.is_offline(offline_error(message)))

    def test_symbol_failure_does_not_block_other_requests(self):
        c=client();bad=response({},200,109418);bad.json.return_value['msg']='PUMPBTC-USDT is offline currently'
        with patch.object(bingx.requests,'get',side_effect=[bad,response({'ok':True})]) as get:
            with self.assertRaises(bingx.BingXUnavailable):c.orderbook('PUMPBTCUSDT',True)
            self.assertTrue(c.enabled)
            self.assertEqual(c.request('futures_depth',{'symbol':'BTC-USDT'}),{'ok':True})
            self.assertEqual(get.call_count,2)

    def test_missing_metadata_not_offline(self):
        c=client()
        with patch.object(c,'contracts',return_value={}):
            with self.assertRaises(bingx.BingXUnavailable) as caught:c.futures_meta('PUMPBTCUSDT')
        self.assertFalse(diag.is_offline(caught.exception))

    def test_explicit_spot_metadata_disabled(self):
        c=client()
        with patch.object(c,'spot_symbols',return_value={'PUMPBTCUSDT':{'apiStateBuy':False}}):
            with self.assertRaises(bingx.BingXUnavailable) as caught:c.spot_rules('PUMPBTCUSDT')
        self.assertTrue(diag.is_offline(caught.exception))

    def test_entry_blocked_with_specific_reason_no_episode(self):
        api=Mock();api.multi_exchange=True;api.dec=Decimal;api.credentials_ready.return_value=True
        api.market_maps.return_value=({'Gate':{'PUMPBTCUSDT':{'lowest_ask':'10'}}},{'BingX':{'PUMPBTCUSDT':{'bidPrice':'11'}}})
        exc=offline_error()
        with patch.object(paper,'storage_ready',return_value=True),patch.object(basis,'evaluate',side_effect=exc),patch.object(basis,'api_blocked_until',{}),patch('entry_policy.reject') as reject,patch.object(paper,'record') as record:
            self.assertEqual(basis.scan(api),[])
            self.assertEqual(reject.call_args.args[0],'REJECTED_SYMBOL_NOT_TRADABLE')
            record.assert_not_called()

    def test_secrets_not_retained(self):
        exc=offline_error('PUMPBTC-USDT is offline currently test-key test-secret signature=abc')
        with self.assertLogs(level='WARNING') as log:diag.emit(exc,21,'PUMPBTCUSDT','observation')
        for value in ('test-key','test-secret','signature=abc'):
            self.assertNotIn(value,' '.join(log.output))


class OfflineObservationTests(unittest.TestCase):
    setUp=test_virtual.VirtualTests.setUp
    state=test_virtual.VirtualTests.state

    def test_natural_failure_preserves_open_and_unknown_no_requests_or_alerts(self):
        exc=offline_error()
        before=self.state()
        with patch.object(virtual,'quote',side_effect=exc),patch.object(virtual,'funding_warnings'),patch.object(virtual,'dispatch'),patch.object(bingx.requests,'get') as get:
            virtual.observe(self.api,self.path)
            virtual.observe(self.api,self.path)
            get.assert_not_called()
        after=self.state()
        self.assertEqual(after['state'],'open')
        self.assertEqual(after['used_capital'],before['used_capital'])
        self.assertIsNone(after['current_json']);self.assertIsNone(after['closed_at'])
        with paper.session(self.path) as db:
            saved=json.loads(db.execute('SELECT value FROM virtual_meta WHERE name=?',(f'quote_availability:{self.ident}',)).fetchone()['value'])
            count=db.execute('SELECT COUNT(*) FROM virtual_events').fetchone()[0]
        for key in ('current_executable_quote','current_executable_spread','trading_net_pnl','full_net_pnl'):
            self.assertIsNone(saved[key])
        self.assertEqual(saved['block_reason'],'BINGX_SYMBOL_OFFLINE')
        self.assertEqual(count,0)
