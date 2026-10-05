import json
import unittest
from unittest.mock import Mock, patch
import entry_diagnostics as d
import basis
import mexc
import bingx


class EntryDiagnosticsTests(unittest.TestCase):
    def test_each_category_preserves_original_exception_and_calls_once(self):
        for category in d.CATEGORIES:
            exc = RuntimeError('secret=DO_NOT_LOG signature=DO_NOT_LOG')
            function = Mock(side_effect=exc)
            with self.assertRaises(RuntimeError) as raised:
                d.call(category, function)
            self.assertIs(raised.exception, exc)
            self.assertEqual(exc.missing_mandatory_data, [category])
            function.assert_called_once_with()

    def test_stage_does_not_leak_across_candidates(self):
        token = d.STAGE.set('OTHER_MANDATORY_DATA')
        try:
            with self.assertRaises(ValueError):
                d.call('MISSING_SPOT_FEE', Mock(side_effect=ValueError('unknown')))
            self.assertEqual(d.STAGE.get(), 'OTHER_MANDATORY_DATA')
        finally:
            d.STAGE.reset(token)

    def test_nested_exact_field_wins_over_depth_stage(self):
        def rules():
            return d.call('MISSING_MIN_QTY', mexc.number, None, strict=True)
        with self.assertRaises(mexc.MEXCUnavailable) as raised:
            d.call('MISSING_SPOT_DEPTH', rules)
        self.assertEqual(raised.exception.missing_mandatory_data, ['MISSING_MIN_QTY'])

    def test_precision_and_stale_categories(self):
        self.assertEqual(d.classify(ValueError('quantity precision unavailable'), 'MISSING_FUTURES_MARKET_PARAMS'), ['MISSING_PRECISION'])
        self.assertEqual(d.classify(ValueError('stale snapshot'), 'MISSING_SPOT_DEPTH'), ['STALE_SPOT_DATA'])

    def test_scan_totals_direction_and_unknown_net_no_credentials(self):
        token = d.begin()
        try:
            d.candidates([(1,'ABCUSDT','MEXC','Gate',None)])
            with self.assertLogs(level='INFO') as captured:
                d.record('REJECTED_UNAVAILABLE_MANDATORY_DATA', dict(symbol='ABCUSDT',spot='MEXC',future='Gate',missing_mandatory_data=['MISSING_SPOT_FEE']))
                d.completed(1,0)
            payload = json.loads(captured.output[-1].split('Entry diagnostic scan complete: ')[1])
            self.assertEqual(payload['counts']['missing_spot_fee'], 1)
            self.assertEqual(payload['directions']['MEXC->Gate']['candidates'], 1)
            self.assertEqual(payload['recent_missing'][0]['expected_net_pct'], 'UNKNOWN')
            self.assertNotIn('signature', str(payload))
        finally:
            d.CYCLE.reset(token)

    def test_evaluate_fee_failure_keeps_request_sequence_no_extra_http(self):
        api=Mock()
        api.fee.side_effect=mexc.MEXCUnavailable('numeric UNKNOWN')
        with patch.object(basis,'futures_meta',return_value={'multiplier':1}), patch.object(basis,'futures_fee') as fee:
            with self.assertRaises(mexc.MEXCUnavailable) as raised:
                basis.evaluate(api,'ABCUSDT','MEXC','Gate')
        self.assertEqual(raised.exception.missing_mandatory_data,['MISSING_SPOT_FEE'])
        api.fee.assert_called_once_with('MEXC','ABCUSDT')
        fee.assert_not_called()
        api.orderbook.assert_not_called()

    def test_missing_funding_timestamp_exact_category(self):
        c=bingx.Client()
        with patch.object(c,'request',return_value={'symbol':'ABC-USDT','lastFundingRate':'0.001'}):
            with self.assertRaises(bingx.BingXUnavailable) as raised:
                d.call('MISSING_FUNDING',c.fresh_funding,'ABCUSDT')
        self.assertEqual(raised.exception.missing_mandatory_data,['MISSING_FUNDING_TIMESTAMP'])

    def test_multiple_missing_fields_count_once_each(self):
        token=d.begin()
        try:
            d.record('REJECTED_UNAVAILABLE_MANDATORY_DATA',dict(symbol='ABCUSDT',spot='Gate',future='MEXC',missing_mandatory_data=['MISSING_MIN_QTY','MISSING_MIN_QTY','MISSING_STEP_SIZE']))
            counts=d.CYCLE.get()['counts']
            self.assertEqual(counts['missing_min_qty'],1)
            self.assertEqual(counts['missing_step_size'],1)
        finally:
            d.CYCLE.reset(token)
