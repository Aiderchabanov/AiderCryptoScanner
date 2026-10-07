"""Read-only Telegram status tests; no real sender/network."""
import unittest
from decimal import Decimal as D
from unittest.mock import patch
import exchange_capital as cap
import paper
import virtual
import test_virtual


class PerExchangeStatusTests(unittest.TestCase):
    setUp = test_virtual.VirtualTests.setUp
    state = test_virtual.VirtualTests.state

    def test_status_reads_persisted_accounts_and_correct_free_limit(self):
        cap.migrate(cap.VENUES, self.path)
        self.api.credentials_ready.return_value = True
        with patch.object(virtual, 'quote', side_effect=ValueError('quote unavailable')):
            virtual.status(self.api, 'test destination', self.path)
        message = self.api.telegram.call_args.args[0]
        for name in cap.VENUES:
            self.assertIn(name + ' (ACTIVE):', message)
        self.assertEqual(message.count('Balance: 10000.00 USDT'), 4)
        self.assertIn('Used: 50.00 / 5000.00 USDT', message)
        self.assertIn('Free: 9950.00 USDT', message)
        self.assertIn('Available for new legs: 4950.00 USDT', message)
        self.assertIn('Confirmed realized P&L (all history): +0.00 USDT', message)
        self.assertIn('Open episodes: 1', message)
        self.assertIn('Closed complete: 0', message)
        self.assertIn('Closed incomplete: 0', message)
        self.assertNotIn('Initial deposit:', message)
        self.assertNotIn('Current virtual balance:', message)
        self.assertNotIn('/ 500.00 USDT', message)
        self.assertNotIn('Free capital: 0.00', message)
        self.assertEqual(self.api.telegram.call_args.args[1], 'test destination')

    def test_status_does_not_mutate_history_accounts_or_repeat_migration(self):
        cap.migrate(cap.VENUES, self.path)
        with paper.session(self.path) as db:
            history = cap.history_hash(db)
        accounting = virtual.accounting(self.path)
        before = cap.status(cap.VENUES, self.path)
        for _ in range(2):
            virtual.status(self.api, 'test destination', self.path)
        self.assertEqual(cap.status(cap.VENUES, self.path), before)
        self.assertEqual(virtual.accounting(self.path), accounting)
        with paper.session(self.path) as db:
            self.assertEqual(cap.history_hash(db), history)
        self.assertEqual(len(before['migrations']), 4)
        self.assertEqual(before['ledger_count'], 4)

    def test_restart_keeps_status_and_accounts_without_reset(self):
        cap.migrate(cap.VENUES, self.path)
        before = virtual.status_message(self.api, self.path)
        virtual.bootstrap(self.path)
        self.assertEqual(virtual.status_message(self.api, self.path), before)

    def test_production_missing_accounts_not_masked_by_legacy_formatter(self):
        with patch.object(virtual, 'accounting', return_value=virtual.accounting(self.path)), patch.object(cap, 'status', return_value={'accounts': []}):
            message = virtual.status_message(self.api)
        self.assertIn('migration not confirmed', message)
        self.assertNotIn('1000.00', message)
        self.assertNotIn('500.00', message)

    def test_inactive_accounts_not_activated_by_status(self):
        cap.migrate(('Binance', 'Gate'), self.path)
        self.api.credentials_ready.side_effect = lambda name: name == 'Binance'
        message = virtual.status_message(self.api, self.path)
        self.assertIn('Binance (ACTIVE)', message)
        self.assertIn('Gate (INACTIVE)', message)
        self.assertNotIn('BingX (ACTIVE)', message)
        self.assertEqual(set(cap.status((), self.path)['active_exchanges']), set())
        self.assertEqual(len(cap.status((), self.path)['accounts']), 2)

    def test_more_than_old_global_500_is_valid_but_exchange_5000_blocks(self):
        cap.migrate(cap.VENUES, self.path)
        item = dict(self.item, spot_cost=D(30), future_notional=D(30))
        with paper.session(self.path) as db:
            with patch.object(cap, 'occupied', return_value={'Binance': D(300), 'Gate': D(300)}):
                self.assertTrue(cap.admits(db, item))
            with patch.object(cap, 'occupied', return_value={'Binance': D(4990), 'Gate': D(300)}):
                self.assertFalse(cap.admits(db, item))
