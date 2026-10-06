"""PAPER-only entry direction safety; all exchanges are offline fixtures."""
import unittest
from unittest.mock import Mock, patch
import basis
import paper


class MEXCSpotEntryDisabledTests(unittest.TestCase):
    def run_scan(self, venues, empty_spots=()):
        spots={v:{'ABCUSDT':{'askPrice':'10','lowest_ask':'10'}} for v in venues}
        futures={v:{'ABCUSDT':{'bidPrice':'10.5','highest_bid':'10.5'}} for v in venues}
        for venue in empty_spots:spots[venue]={}
        api=Mock();api.multi_exchange=True;api.market_maps.return_value=(spots,futures)
        api.credentials_ready.return_value=True;api.dec=lambda value:__import__('decimal').Decimal(value)
        with patch.object(paper,'storage_ready',return_value=True),patch.object(basis,'evaluate',return_value=None) as evaluate,patch.object(basis,'api_blocked_until',{}),self.assertLogs(level='INFO') as logs:
            basis.scan(api)
        self.assertIn('MEXC_SPOT_QUANTITY_RULES_UNCONFIRMED',' '.join(logs.output))
        self.assertEqual(set(spots),set(venues))
        self.assertEqual(set(futures),set(venues))
        api.telegram.assert_not_called()
        return {call.args[2:4] for call in evaluate.call_args_list}

    def test_mexc_spot_excluded_future_venue_preserved(self):
        venues=('Binance','Gate','BingX','MEXC')
        pairs=self.run_scan(venues)
        self.assertEqual(pairs,{(s,f) for s in venues for f in venues if s!='MEXC' and s!=f})
        for spot in ('Binance','Gate','BingX'):self.assertIn((spot,'MEXC'),pairs)
        self.assertFalse(any(spot=='MEXC' for spot,_ in pairs))

    def test_future_venues_do_not_bypass_spot_safety_gate(self):
        pairs=self.run_scan(('Gate','BingX','MEXC','FutureVenue'))
        self.assertNotIn(('MEXC','FutureVenue'),pairs)
        self.assertIn(('FutureVenue','MEXC'),pairs)

    def test_unavailable_binance_spot_does_not_disable_gate_bingx(self):
        pairs=self.run_scan(('Binance','Gate','BingX','MEXC'),('Binance',))
        self.assertNotIn(('Binance','MEXC'),pairs)
        self.assertIn(('Gate','MEXC'),pairs)
        self.assertIn(('BingX','MEXC'),pairs)
