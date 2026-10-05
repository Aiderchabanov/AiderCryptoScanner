import unittest
from unittest.mock import patch
import mexc
import entry_diagnostics as d

class SnapshotTests(unittest.TestCase):
    def test_failure_unchanged_deduplicated_and_credentials_excluded(self):
        token=d.begin();c=mexc.Client()
        row={'tradeSideType':1,'baseAssetPrecision':2,'baseSizePrecision':'0','api_key':'SECRET','signature':'SECRET','quoteAmountPrecision':'signature=SECRET'}
        try:
            with patch.object(c,'spot_symbols',return_value={'ABCUSDT':row}), self.assertLogs(level='INFO') as logs, patch.object(mexc.requests,'get') as get:
                for _ in range(2):
                    with self.assertRaises(mexc.MEXCUnavailable) as raised:c.spot_rules('ABCUSDT')
                    self.assertEqual(raised.exception.missing_mandatory_data,['MISSING_MIN_QTY'])
                get.assert_not_called()
            self.assertEqual(len(logs.output),1)
            self.assertNotIn('SECRET',str(logs.output))
            self.assertIn('REJECTED_FIELD_ZERO',str(logs.output))
        finally:d.CYCLE.reset(token)

    def test_success_and_other_missing_fields_do_not_log(self):
        token=d.begin();c=mexc.Client()
        try:
            with patch.object(c,'spot_symbols',return_value={'ABCUSDT':{'tradeSideType':1,'baseAssetPrecision':2,'baseSizePrecision':'0.01','quoteAmountPrecision':'1'}}),patch.object(mexc.logging,'info') as log:
                self.assertEqual(str(c.spot_rules('ABCUSDT')['min_qty']),'0.01');log.assert_not_called()
            with patch.object(c,'spot_symbols',return_value={'ABCUSDT':{'tradeSideType':1,'baseAssetPrecision':2,'baseSizePrecision':'0.01'}}),patch.object(mexc.logging,'info') as log:
                with self.assertRaises(mexc.MEXCUnavailable):c.spot_rules('ABCUSDT')
                log.assert_not_called()
        finally:d.CYCLE.reset(token)

    def test_futures_and_outside_scan_do_not_log(self):
        c=mexc.Client()
        with patch.object(c,'spot_symbols',return_value={'ABCUSDT':{'tradeSideType':1,'baseAssetPrecision':2,'baseSizePrecision':None}}),patch.object(mexc.logging,'info') as log:
            with self.assertRaises(mexc.MEXCUnavailable):c.spot_rules('ABCUSDT')
            log.assert_not_called()
        token=d.begin()
        try:
            with patch.object(c,'contracts',return_value={}),patch.object(mexc.logging,'info') as log:
                with self.assertRaises(mexc.MEXCUnavailable):c.futures_meta('ABCUSDT')
                log.assert_not_called()
        finally:d.CYCLE.reset(token)

    def test_new_cycle_rearms_snapshot(self):
        for _ in range(2):
            token=d.begin()
            try:
                with self.assertLogs(level='INFO') as logs:mexc.Client.spot_metadata_snapshot('ABCUSDT',{},'MISSING_SPOT_MARKET_PARAMS')
                self.assertEqual(len(logs.output),1)
            finally:d.CYCLE.reset(token)
