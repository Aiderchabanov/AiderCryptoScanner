"""Isolated PAPER migration/reservation/restart regressions, no network."""
import json
import unittest
from decimal import Decimal as D
from unittest.mock import patch
import basis
import exchange_capital as cap
import paper
import virtual
import test_virtual


class ExchangeCapitalTests(unittest.TestCase):
    setUp = test_virtual.VirtualTests.setUp
    state = test_virtual.VirtualTests.state

    def migrate(self, names=cap.VENUES):
        return cap.migrate(names, self.path)

    def status(self):
        return {r['exchange']: r for r in cap.status(cap.VENUES, self.path)['accounts']}

    def test_active_only_no_client_activation(self):
        self.api.credentials_ready.side_effect = lambda name: name in ('Binance', 'Gate')
        self.assertEqual(cap.active(self.api), ('Binance', 'Gate'))
        self.migrate(cap.active(self.api))
        self.assertEqual(set(self.status()), {'Binance', 'Gate'})
        self.api.orderbook.assert_not_called()
        self.api.telegram.assert_not_called()
        with self.assertRaises(ValueError): self.migrate(('Bybit',))

    def test_10000_each_history_hash_and_restart_idempotency(self):
        with paper.session(self.path) as db: before = cap.history_hash(db)
        pnl_before = virtual.accounting(self.path)['confirmed_realized_pnl']
        self.assertEqual(self.migrate(), list(cap.VENUES))
        first = cap.status(cap.VENUES, self.path)
        self.assertEqual(self.migrate(), [])
        self.assertEqual(cap.status(cap.VENUES, self.path), first)
        virtual.bootstrap(self.path)
        self.assertEqual(cap.status(cap.VENUES, self.path), first)
        with paper.session(self.path) as db:
            self.assertEqual(cap.history_hash(db), before)
            audits = db.execute("SELECT * FROM scanner_capital_ledger WHERE kind='ACCOUNT_INIT'").fetchall()
            self.assertEqual(len(audits), 4)
            for r in audits:
                self.assertIsNone(r['balance_before'])
                self.assertEqual(r['balance_after'], '10000')
                self.assertEqual(r['reason'], cap.MIGRATION)
                self.assertEqual(json.loads(r['details_json'])['history_hash_before'], before)
        for r in self.status().values():
            self.assertEqual(D(r['virtual_balance']), D(10000))
            self.assertEqual(D(r['max_allowed_used_capital']), D(5000))
        self.assertEqual(virtual.accounting(self.path)['confirmed_realized_pnl'], pnl_before)

    def test_each_leg_reserves_own_exchange_and_free_funds(self):
        self.migrate()
        rows = self.status()
        self.assertEqual(D(rows['Binance']['used_capital']), D(50))
        self.assertEqual(D(rows['Gate']['used_capital']), D('50.9'))
        self.assertEqual(D(rows['BingX']['used_capital']), 0)
        self.assertEqual(D(rows['Binance']['free_capital']), D(9950))
        self.assertEqual(D(rows['Binance']['admission_available_capital']), D(4950))
        self.assertEqual(rows['Binance']['open_episodes_using_exchange'], 1)

    def test_no_cross_exchange_subsidy_both_legs_must_fit(self):
        self.migrate()
        with paper.session(self.path) as db:
            item = dict(self.item, spot='BingX', future='MEXC', spot_cost=D(30), future_notional=D(30))
            with patch.object(cap, 'occupied', return_value={'MEXC': D(4990)}):
                self.assertFalse(cap.admits(db, item))
            with patch.object(cap, 'occupied', return_value={'BingX': D(4990)}):
                self.assertFalse(cap.admits(db, item))
            with patch.object(cap, 'occupied', return_value={'BingX': D(4970), 'MEXC': D(4970)}):
                self.assertTrue(cap.admits(db, item))
            self.assertFalse(cap.admits(db, dict(item, future='Bybit')))

    def test_new_episode_uses_per_exchange_not_legacy_500(self):
        self.migrate()
        item = dict(self.item, symbol='CAPITALUSDT', paper_budget_usdt=D(30), spot_cost=D(30), future_notional=D(30))
        with patch.object(basis, 'LEG_USDT', D(30)), patch.object(virtual, 'used', return_value=D(900)):
            self.assertIsNotNone(paper.record(item, self.path))
        self.assertEqual(D(self.status()['Binance']['used_capital']), D(80))
        self.assertEqual(basis.TARGET_LEG_USDT, D(30))

    def test_future_margin_uses_saved_leverage_on_restart(self):
        with paper.session(self.path) as db:
            db.execute('UPDATE virtual_meta SET value=? WHERE name=?', ('2', f'capital_leverage:{self.ident}'))
        self.migrate()
        with patch.object(virtual, 'leverage', return_value=D(5)):
            self.assertEqual(D(self.status()['Gate']['used_capital']), D('25.45'))
            self.migrate()
            self.assertEqual(D(self.status()['Gate']['used_capital']), D('25.45'))

    def close(self, known=True):
        q = {'at': __import__('time').time(), 'spread': '.08', 'spot_exit': '10', 'future_exit': '10.1',
             'spot_pnl': '1', 'futures_pnl': '-.2', 'funding_realized_usdt': '.1' if known else None,
             'total_trading_fees': '.1', 'net_pnl': '.9' if known else None, 'trading_net': '.8',
             'funding_status': 'known' if known else 'UNKNOWN_SETTLEMENT_PNL'}
        e = virtual.load(self.ident, self.path)
        with paper.session(self.path) as db:
            self.assertTrue(virtual.close_db(db, e, q))
            self.assertFalse(virtual.close_db(db, e, q))
        return q

    def test_known_close_leg_pnl_once_release_restart(self):
        self.migrate()
        self.close()
        rows = self.status()
        self.assertEqual(D(rows['Binance']['virtual_balance']), D(10001))
        self.assertEqual(D(rows['Gate']['virtual_balance']), D('9999.9'))
        self.assertEqual(D(rows['Binance']['used_capital']), 0)
        self.assertEqual(D(rows['Gate']['used_capital']), 0)
        self.assertEqual(virtual.accounting(self.path)['confirmed_realized_pnl'], D('.9'))
        before = cap.status(cap.VENUES, self.path)
        self.migrate(); virtual.bootstrap(self.path)
        self.assertEqual(cap.status(cap.VENUES, self.path), before)

    def test_unknown_close_never_invents_pnl(self):
        self.migrate(); self.close(False)
        for r in self.status().values(): self.assertEqual(D(r['virtual_balance']), D(10000))
        self.assertEqual(D(self.status()['Binance']['used_capital']), 0)

    def test_historical_closed_pnl_not_reallocated(self):
        self.close()
        with paper.session(self.path) as db: before = cap.history_hash(db)
        pnl = virtual.accounting(self.path)['confirmed_realized_pnl']
        self.migrate()
        with paper.session(self.path) as db: self.assertEqual(cap.history_hash(db), before)
        for r in self.status().values(): self.assertEqual(D(r['virtual_balance']), D(10000))
        self.assertEqual(virtual.accounting(self.path)['confirmed_realized_pnl'], pnl)

    def test_production_unmigrated_fails_closed(self):
        class Production:
            is_postgres = True
        with patch.object(cap, 'balances', return_value={}):
            self.assertFalse(cap.admits(Production(), self.item))

