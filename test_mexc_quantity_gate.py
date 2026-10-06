"""Offline fixtures only. No fixture asserts production exchange semantics."""
import unittest
from decimal import Decimal as D
from unittest.mock import Mock, patch
import mexc
import entry_diagnostics as diagnostics


class QuantityGateTests(unittest.TestCase):
    def test_confirmed_minimum_above_target_remains_rejected(self):
        import basis, entry_policy
        api=Mock();api.dec=lambda value:D(str(value));api.fee.return_value=D('.001')
        api.orderbook.return_value=([['10','100']],[['9.99','100']])
        api.spot_rules.return_value={**mexc.Client.validate_spot_quantity_rules('.01','.01'),'min_quote':D(31)}
        meta={'min_qty':D('.01'),'step':D('.01'),'min_notional':D(1),'multiplier':D(1)}
        with patch.object(basis,'futures_meta',return_value=meta),patch.object(basis,'futures_fee',return_value=D('.001')),patch.object(basis,'futures_book',return_value=([['10.31','100']],[['10.3','100']])),patch.object(entry_policy,'reject') as reject:
            self.assertIsNone(basis.evaluate(api,'ABCUSDT','MEXC','Gate'))
            self.assertEqual(reject.call_args.args[0],'REJECTED_MIN_ORDER_ABOVE_TARGET')

    def test_confirmed_values_pass_numeric_gate(self):
        self.assertEqual(mexc.Client.validate_spot_quantity_rules('0.01', '0.001'),
                         {'min_qty':D('.01'),'step':D('.001')})

    def test_zero_not_integer_or_unrestricted(self):
        with self.assertRaises(mexc.MEXCUnavailable) as raised:
            mexc.Client.validate_spot_quantity_rules('0', None)
        self.assertEqual(raised.exception.mexc_spot_quantity_reasons,
                         ['MEXC_SPOT_UNKNOWN_MIN_QTY','MEXC_SPOT_UNKNOWN_QUANTITY_INCREMENT'])

    def test_precision_never_supplies_increment(self):
        c=mexc.Client()
        for precision in (0, 2, 8):
            with self.subTest(precision=precision),patch.object(c,'spot_symbols',return_value={
                'ABCUSDT':{'tradeSideType':1,'baseAssetPrecision':precision,
                           'baseSizePrecision':'1','quoteAmountPrecision':'1'}}):
                with self.assertRaises(mexc.MEXCUnavailable) as raised:c.spot_rules('ABCUSDT')
                self.assertEqual(raised.exception.missing_mandatory_data,['MISSING_STEP_SIZE'])

    def test_unknown_and_invalid_never_fallback(self):
        for value in (None,'','abc','0','-1','NaN','Infinity'):
            with self.subTest(value=value),self.assertRaises(mexc.MEXCUnavailable):
                mexc.Client.validate_spot_quantity_rules(value, '.01')
            with self.subTest(step=value),self.assertRaises(mexc.MEXCUnavailable):
                mexc.Client.validate_spot_quantity_rules('1', value)

    def test_futures_independent_of_spot_failure(self):
        c=mexc.Client()
        with patch.object(c,'contracts',return_value={'ABCUSDT':{
            'minVol':'2','volUnit':'1','contractSize':'.01'}}),patch.object(c,'spot_symbols',return_value={}),patch.object(mexc.requests,'get') as get:
            with self.assertRaises(mexc.MEXCUnavailable):c.spot_rules('ABCUSDT')
            self.assertEqual(c.futures_meta('ABCUSDT')['min_qty'],D('.02'))
            self.assertEqual(c.futures_meta('ABCUSDT')['step'],D('.01'))
            get.assert_not_called()

    def test_reasons_logged_only_on_mexc_spot(self):
        token=diagnostics.begin()
        try:
            with self.assertLogs(level='INFO') as logs:
                diagnostics.record('REJECTED_UNAVAILABLE_MANDATORY_DATA',{
                    'symbol':'ABCUSDT','spot':'MEXC','future':'Gate',
                    'missing_mandatory_data':['MISSING_STEP_SIZE'],
                    'mexc_spot_quantity_reasons':['MEXC_SPOT_UNKNOWN_QUANTITY_INCREMENT','secret=value']})
            self.assertIn('MEXC_SPOT_UNKNOWN_QUANTITY_INCREMENT',str(logs.output))
            self.assertNotIn('secret=value',str(logs.output))
            self.assertEqual(diagnostics.CYCLE.get()['directions']['MEXC->Gate']['symbols_rejected_unknown_mexc_quantity'],1)
        finally:diagnostics.CYCLE.reset(token)
