"""Offline evidence fixtures only; never creates production episodes."""
import json
import tempfile
import unittest
from decimal import Decimal as D
from unittest.mock import MagicMock, patch

import funding_accounting as funding
import bingx
import mexc
import paper
import virtual
import test_virtual


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.e = dict(id=26, episode_id=26, symbol='ZCATUSDT',
                      futures_exchange='BingX', quantity='10', started_at=100,
                      next_funding_at=200, direction='spot_buy/futures_short', state='open')

    def record(self, stamp=200, rate='.01', price='2'):
        # Hypothetical officially proven valuation fixture. Live adapters cannot
        # produce valuation_verified=True without an official field definition.
        return dict(symbol='ZCATUSDT', settlement_at=stamp, rate=rate,
                    settlement_price=price, valuation_verified=True,
                    source=funding.SOURCES['BingX'])

    def test_before_first_funding(self):
        q = funding.evaluate(self.e, [], False, 199)
        self.assertEqual(q['funding_realized_usdt'], '0')
        self.assertEqual(q['funding_status'], 'no settlement crossed')

    def test_one_positive_and_negative_settlement(self):
        for rate, expected in (('.01', '.20'), ('-.01', '-.20'), ('0', '0')):
            q = funding.evaluate(self.e, [self.record(rate=rate)], True, 201)
            self.assertEqual(D(q['funding_realized_usdt']), D(expected))
            self.assertEqual(q['funding_status'], 'CONFIRMED_PAPER_FUNDING')

    def test_multiple_settlements_no_fixed_schedule_assumption(self):
        q = funding.evaluate(self.e, [self.record(), self.record(350, '-.02', '3')], True, 351)
        self.assertEqual(D(q['funding_realized_usdt']), D('-.4'))

    def test_missing_coverage_or_first_event_unknown(self):
        for records, complete in (([], True), ([self.record()], False), ([self.record(350)], True)):
            q = funding.evaluate(self.e, records, complete, 351)
            self.assertIsNone(q['funding_realized_usdt'])

    def test_unknown_valuation_and_current_rate_never_substituted(self):
        r = self.record(); r['valuation_verified'] = False
        q = funding.evaluate(self.e, [r], True, 201)
        self.assertIsNone(q['funding_realized_usdt'])
        self.assertEqual(q['funding_unknown_reason'], 'SETTLEMENT_VALUATION_UNVERIFIED')

    def test_duplicate_and_foreign_evidence_rejected(self):
        self.assertIsNone(funding.evaluate(self.e, [self.record()] * 2, True, 201)['funding_realized_usdt'])
        r = self.record(); r['source'] = 'https://unverified.invalid'
        self.assertIsNone(funding.evaluate(self.e, [r], True, 201)['funding_realized_usdt'])

    def test_live_bingx_undocumented_mark_never_confirmed(self):
        r = funding.normalized(dict(symbol='ZCAT-USDT', fundingTime=200000,
                                     fundingRate='.01', markPrice='2'), 'BingX', 'ZCATUSDT')
        self.assertFalse(r['valuation_verified'])
        self.assertIsNone(funding.evaluate(self.e, [r], True, 201)['funding_realized_usdt'])

    def test_mexc_rate_only_never_confirmed(self):
        r = funding.normalized(dict(symbol='ZCAT_USDT', settleTime=200000,
                                     fundingRate='.01'), 'MEXC', 'ZCATUSDT')
        self.e['futures_exchange'] = 'MEXC'
        self.assertIsNone(funding.evaluate(self.e, [r], True, 201)['funding_realized_usdt'])

    def test_malformed_timestamps_rate_symbol_rejected(self):
        for changes in ({'fundingTime': None}, {'fundingRate': 'NaN'}, {'fundingRate': '.5'}, {'symbol': 'OTHER-USDT'}):
            row = dict(symbol='ZCAT-USDT', fundingTime=200000, fundingRate='.01')
            row.update(changes)
            with self.assertRaises(Exception): funding.normalized(row, 'BingX', 'ZCATUSDT')

    def test_history_pagination_and_rate_only_provenance(self):
        api = MagicMock(); api.bingx.cached.side_effect = lambda k,t,f: f()
        api.bingx.request.return_value = [dict(symbol='ZCAT-USDT', fundingTime=200000, fundingRate='.01')]
        rows, complete, covered = funding.history(api, self.e, 201)
        self.assertTrue(complete); self.assertEqual(covered, 201)
        self.assertFalse(rows[0]['valuation_verified'])
        self.assertEqual(api.bingx.request.call_args.args[0], 'funding_history')
        api.bingx.request.return_value[0]['fundingTime'] = 300000
        with self.assertRaises(ValueError): funding.history(api, self.e, 201)

    def test_history_failure_isolated_closed_immutable(self):
        api = MagicMock()
        with patch.object(funding, 'history', side_effect=RuntimeError('private credential fixture')):
            q = funding.snapshot(api, self.e, now=201)
        self.assertIsNone(q['funding_realized_usdt'])
        self.assertNotIn('credential', str(q))
        self.e['state'] = 'closed'
        with patch.object(funding, 'history') as fetch:
            funding.snapshot(api, self.e, now=201); fetch.assert_not_called()

    def test_history_transport_public_get_only_no_credentials(self):
        for module, exchange in ((bingx, 'BingX'), (mexc, 'MEXC')):
            client = module.Client(); client.enabled = True
            client.key = 'fixture-key'; client.secret = 'fixture-secret'
            response = MagicMock(status_code=200)
            response.json.return_value = {'code': 0, 'success': True, 'data': []}
            with patch.object(module.requests, 'get', return_value=response) as request:
                client.request('funding_history', {'symbol': module.pair('ZCATUSDT'), 'limit': 1} if exchange == 'BingX' else {'symbol': module.pair('ZCATUSDT'), 'page_num': 1, 'page_size': 1})
            sent = request.call_args.kwargs
            self.assertNotIn('signature', sent['params'])
            self.assertNotIn('fixture-key', str(sent))
            self.assertEqual(request.call_args.args[0], funding.SOURCES[exchange])
            self.assertFalse(sent['allow_redirects'])

    def test_cached_history_is_not_current_complete_coverage(self):
        with patch.object(funding, 'history', return_value=([self.record()], True, 201)), patch.object(funding, 'persist'):
            q = funding.snapshot(MagicMock(), self.e, now=202)
        self.assertIsNone(q['funding_realized_usdt'])

    def test_restart_persistence_idempotency_and_immutable_conflict(self):
        with tempfile.TemporaryDirectory() as temp:
            path = temp + '/paper.db'
            # Existing schema fixture, no production URL or database access.
            with patch.dict('os.environ', {'PAPER_DATABASE_URL': ''}):
                with paper.session(path) as db: virtual.ensure(db)
                r = self.record()
                funding.persist(self.e, [r], True, 201, path)
                funding.persist(self.e, [r], True, 201, path)
                with paper.session(path) as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM virtual_meta WHERE name LIKE 'paper_funding_v1:%'").fetchone()[0], 1)
                    value = json.loads(db.execute("SELECT value FROM virtual_meta WHERE name LIKE 'paper_funding_v1:%'").fetchone()['value'])
                    self.assertEqual(value['quantity'], '10')
                r['rate'] = '.02'
                with self.assertRaises(ValueError): funding.persist(self.e, [r], True, 201, path)


class QuoteIntegration(unittest.TestCase):
    setUp = test_virtual.VirtualTests.setUp
    quote = test_virtual.VirtualTests.quote
    state = test_virtual.VirtualTests.state
    def test_full_net_adds_confirmed_funding_without_double_fees(self):
        e = virtual.load(self.ident, self.path)
        base = virtual.quote(self.api, e, self.path)
        for value in ('.2', '-.2'):
            evidence = dict(funding_status='CONFIRMED_PAPER_FUNDING',
                            funding_realized_usdt=value, funding_unknown_reason=None,
                            funding_settlements_seen=1)
            with patch.object(funding, 'snapshot', return_value=evidence):
                q = virtual.quote(self.api, e, self.path)
            self.assertEqual(D(q['net_pnl']), D(base['trading_net']) + D(value))
            self.assertEqual(q['total_trading_fees'], base['total_trading_fees'])

    def test_quote_crossing_settlement_does_not_keep_zero(self):
        e = virtual.load(self.ident, self.path)
        e['next_funding_at'] = 200; e['started_at'] = 100
        with patch.object(funding.time, 'time', side_effect=[199, 201, 201]):
            q = virtual.quote(self.api, e, self.path)
        self.assertIsNone(q['net_pnl'])

    def test_unknown_net_prevents_auto_close(self):
        q = self.quote('.08'); q.update(funding.unknown('SETTLEMENT_VALUATION_UNVERIFIED'))
        q['net_pnl'] = None
        with patch.object(virtual, 'quote', return_value=q): virtual.observe(self.api, self.path)
        self.assertEqual(self.state()['state'], 'open')


if __name__ == '__main__': unittest.main()
