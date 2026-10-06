"""Regression checks of the existing Spot parser; all metadata are test fixtures."""
import unittest
from decimal import Decimal
from unittest.mock import patch
import mexc


class MEXCSpotMinQtyTests(unittest.TestCase):
    def check_value(self, value, expected):
        client = mexc.Client()
        metadata = {'symbol':'BOMEUSDT', 'tradeSideType':1,
                    'baseAssetPrecision':2, 'baseSizePrecision':value,
                    'quoteAmountPrecision':'1'}
        with patch.object(client, 'spot_symbols', return_value={'BOMEUSDT':metadata}), \
             patch.object(mexc.requests, 'get') as get:
            if expected is None:
                with self.assertRaises(mexc.MEXCUnavailable) as raised:
                    client.spot_rules('BOMEUSDT')
                self.assertIn('MISSING_MIN_QTY', raised.exception.missing_mandatory_data)
            else:
                # Parsing a positive minimum does not establish an increment.
                self.assertEqual(mexc.number(value, strict=True), Decimal(expected))
                with self.assertRaises(mexc.MEXCUnavailable) as raised:
                    client.spot_rules('BOMEUSDT')
                self.assertEqual(raised.exception.missing_mandatory_data, ['MISSING_STEP_SIZE'])
            get.assert_not_called()

    def test_numeric_string_fraction(self): self.check_value('0.01', '0.01')
    def test_numeric_string_integer(self): self.check_value('1', '1')
    def test_numeric_float(self): self.check_value(0.01, '0.01')
    def test_null(self): self.check_value(None, None)
    def test_empty(self): self.check_value('', None)
    def test_non_numeric(self): self.check_value('abc', None)
    def test_zero(self): self.check_value('0', None)
    def test_negative(self): self.check_value('-1', None)

    def test_non_finite_and_missing(self):
        for value in ('NaN','Infinity','-Infinity'):
            with self.subTest(value=value): self.check_value(value, None)
        client=mexc.Client()
        with patch.object(client,'spot_symbols',return_value={'BOMEUSDT':{'tradeSideType':1,'baseAssetPrecision':2}}):
            with self.assertRaises(mexc.MEXCUnavailable) as raised:
                client.spot_rules('BOMEUSDT')
            self.assertIn('MISSING_MIN_QTY',raised.exception.missing_mandatory_data)
