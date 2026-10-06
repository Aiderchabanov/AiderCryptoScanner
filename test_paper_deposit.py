"""PAPER-only deposit regressions; isolated SQLite and mocked network clients."""
import os
import unittest
from decimal import Decimal as D
from unittest.mock import patch
import basis, paper, virtual, entry_diagnostics as diagnostics
import test_virtual


class DepositTests(unittest.TestCase):
    setUp = test_virtual.VirtualTests.setUp
    state = test_virtual.VirtualTests.state

    def test_default_deposit_limit_and_unchanged_target(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(virtual.deposit(), D(1000))
            self.assertEqual(virtual.working_capital_limit(), D(500))
        self.assertEqual(basis.TARGET_LEG_USDT, D(30))

    def test_half_deposit_and_absolute_working_limit(self):
        with patch.dict(os.environ, PAPER_DEPOSIT_USDT='1000'):
            self.assertEqual(virtual.working_capital_limit(), D(500))
        with patch.dict(os.environ, PAPER_DEPOSIT_USDT='2000'):
            self.assertEqual(virtual.working_capital_limit(), D(500))

    def test_deposit_change_preserves_saved_open_episode(self):
        before = self.state()
        with paper.session(self.path) as db:
            entry_before = dict(db.execute('SELECT * FROM episodes WHERE id=?', (self.ident,)).fetchone())
        with patch.dict(os.environ, PAPER_DEPOSIT_USDT='500'):
            old = virtual.accounting(self.path)
        with patch.dict(os.environ, PAPER_DEPOSIT_USDT='1000'):
            virtual.bootstrap(self.path)
            new = virtual.accounting(self.path)
        self.assertEqual(old['used_capital'], new['used_capital'])
        self.assertEqual(before['used_capital'], self.state()['used_capital'])
        self.assertEqual(new['initial_virtual_deposit'], D(1000))
        self.assertEqual(new['free_capital'], D(500)-new['used_capital'])
        with paper.session(self.path) as db:
            self.assertEqual(entry_before, dict(db.execute('SELECT * FROM episodes WHERE id=?', (self.ident,)).fetchone()))

    def test_new_entry_can_use_remaining_capital_above_old_limit(self):
        item = dict(self.item, symbol='DEPOSITUSDT', paper_budget_usdt=D(30), spot_cost=D(30), future_notional=D(30))
        with patch.dict(os.environ, PAPER_DEPOSIT_USDT='1000'), patch.object(basis, 'LEG_USDT', D(30)), patch.object(virtual, 'used', return_value=D(400)), patch.object(virtual, 'leverage', return_value=D(1)):
            self.assertIsNotNone(paper.record(item, self.path))
        self.api.orderbook.assert_not_called()
        self.api.telegram.assert_not_called()

    def test_over_500_uses_existing_capital_block(self):
        item = dict(self.item, symbol='LIMITUSDT', paper_budget_usdt=D(30), spot_cost=D(30), future_notional=D(30))
        token = diagnostics.begin()
        try:
            with patch.dict(os.environ, PAPER_DEPOSIT_USDT='1000'), patch.object(basis, 'LEG_USDT', D(30)), patch.object(virtual, 'used', return_value=D(450)), patch.object(virtual, 'leverage', return_value=D(1)):
                self.assertIsNone(paper.record(item, self.path))
            self.assertEqual(diagnostics.CYCLE.get()['flow_events'][-1]['reason'], 'MAX_WORKING_CAPITAL')
        finally:
            diagnostics.CYCLE.reset(token)
        self.assertEqual(self.state()['state'], 'open')
