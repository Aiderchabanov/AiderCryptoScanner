"""Notification-only regressions; temporary SQLite and mocked clients."""
import json
import time
import unittest
from decimal import Decimal as D
from unittest.mock import patch
import paper
import virtual
import test_virtual


class FundingWarningSignTests(unittest.TestCase):
    setUp = test_virtual.VirtualTests.setUp
    state = test_virtual.VirtualTests.state

    def event_count(self):
        with paper.session(self.path) as db:
            return db.execute("SELECT COUNT(*) FROM virtual_events WHERE event_key LIKE 'funding:%'").fetchone()[0]

    def test_positive_and_zero_do_not_send_or_claim(self):
        for rate in (D('.0001'), D(0)):
            self.funding.return_value = (rate, time.time() + 1700)
            virtual.funding_warnings(self.api, self.path)
        self.api.telegram.assert_not_called()
        self.assertEqual(self.event_count(), 0)
        self.assertEqual(self.state()['state'], 'open')

    def test_negative_once_then_next_event_once_restart(self):
        deadline = time.time() + 1600
        self.funding.return_value = (D('-.0001'), deadline)
        virtual.funding_warnings(self.api, self.path)
        virtual.funding_warnings(self.api, self.path)
        virtual.bootstrap(self.path)
        virtual.funding_warnings(self.api, self.path)
        self.assertEqual(self.api.telegram.call_count, 1)
        self.assertIn('Funding: -0.0100%', self.api.telegram.call_args.args[0])
        self.funding.return_value = (D('-.0001'), deadline + 100)
        virtual.funding_warnings(self.api, self.path)
        virtual.funding_warnings(self.api, self.path)
        self.assertEqual(self.api.telegram.call_count, 2)
        self.assertEqual(self.event_count(), 2)

    def test_unknown_nonfinite_unavailable_do_not_send(self):
        for rate in (None, D('NaN'), D('Infinity')):
            self.funding.return_value = (rate, time.time() + 1700)
            virtual.funding_warnings(self.api, self.path)
        self.funding.side_effect = ValueError('fresh funding unavailable')
        virtual.funding_warnings(self.api, self.path)
        self.api.telegram.assert_not_called()
        self.assertEqual(self.event_count(), 0)

    def test_stale_or_future_dated_snapshot_does_not_send(self):
        for stamp in (time.time() - 31, time.time() + 31):
            q = {'at': stamp, 'rate': '-.0001', 'next_at': time.time() + 1700}
            with patch.object(virtual, 'funding_snapshot', return_value=q):
                virtual.funding_warnings(self.api, self.path)
        self.api.telegram.assert_not_called()
        self.assertEqual(self.event_count(), 0)

    def test_slow_funding_refresh_does_not_send(self):
        self.funding.return_value = (D('-.0001'), time.time() + 1700)
        with patch.object(virtual.time, 'monotonic', side_effect=[100, 131]):
            virtual.funding_warnings(self.api, self.path)
        self.api.telegram.assert_not_called()
        self.assertEqual(self.event_count(), 0)

    def test_suppressed_positive_does_not_consume_negative_event(self):
        deadline = time.time() + 1700
        self.funding.return_value = (D('.0001'), deadline)
        virtual.funding_warnings(self.api, self.path)
        self.funding.return_value = (D('-.0001'), deadline)
        virtual.funding_warnings(self.api, self.path)
        self.assertEqual(self.api.telegram.call_count, 1)
        self.assertEqual(self.event_count(), 1)

    def test_entry_admission_stays_positive_and_episode_unchanged(self):
        import entry_policy
        with paper.session(self.path) as db:
            before = dict(db.execute('SELECT * FROM episodes WHERE id=?', (self.ident,)).fetchone())
        accounting = virtual.accounting(self.path)
        self.funding.return_value = (D('-.0001'), time.time() + 1700)
        virtual.funding_warnings(self.api, self.path)
        with paper.session(self.path) as db:
            self.assertEqual(dict(db.execute('SELECT * FROM episodes WHERE id=?', (self.ident,)).fetchone()), before)
        self.assertEqual(virtual.accounting(self.path), accounting)
        self.assertIsNone(entry_policy.reason(self.item))
        self.assertEqual(entry_policy.reason(dict(self.item, funding=D('-.0001'))), 'REJECTED_NEGATIVE_FUNDING')
        self.assertEqual(self.state()['state'], 'open')
        self.api.orderbook.assert_called()
