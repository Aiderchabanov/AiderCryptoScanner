import tempfile,time,unittest
from pathlib import Path
from decimal import Decimal as D
from unittest.mock import patch
import app,basis,paper,virtual
from test_paper import example

class TargetSizeTests(unittest.TestCase):
    def evaluate(self,min_spot=D(5),min_future=D(5),future_step=D('.01')):
        meta={'step':future_step,'min_qty':future_step,'min_notional':min_future,'multiplier':D(1)}
        rules={'min_qty':D('.01'),'min_quote':min_spot,'step':D('.00000001')}
        with patch.object(basis,'futures_meta',return_value=meta),patch.object(basis,'futures_fee',return_value=D('.001')),patch.object(app,'fee',return_value=D('.001')),patch.object(app,'spot_rules',return_value=rules),patch.object(app,'orderbook',return_value=([['10','100']],[['9.99','100']])),patch.object(basis,'futures_book',return_value=([['10.31','100']],[['10.3','100']])),patch.object(basis,'fresh_funding',return_value=(D('.0001'),time.time()+18000)):
            return basis.evaluate(app,'ABCUSDT','Binance','Gate')
    def test_thirty_target_real_executable_notionals_and_quantization(self):
        item=self.evaluate()
        self.assertIsNotNone(item)
        self.assertEqual(item['paper_budget_usdt'],D(30))
        self.assertLessEqual(item['spot_cost'],D(30));self.assertLessEqual(item['future_notional'],D(30))
        self.assertEqual(item['quantity']%D('.01'),0)
    def test_each_minimum_above_target_blocks_without_upsizing(self):
        for args in ({'min_spot':D(31)},{'min_future':D(31)},{'future_step':D(4)}):
            with self.assertLogs(level='INFO') as logs:self.assertIsNone(self.evaluate(**args))
            self.assertIn('REJECTED_MIN_ORDER_ABOVE_TARGET',' '.join(logs.output))
    def test_four_full_pairs_fifth_blocked_actual_capital_and_target_saved(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict('os.environ',{'PAPER_FUTURES_LEVERAGE':'1'}):
            path=Path(tmp)/'isolated.sqlite'
            item=dict(example(),paper_budget_usdt=D(30),spot_cost=D(30),future_notional=D(30),quantity=D(3),executable_spread_pct=D('1.8'),raw_spread_pct=D(2),next_funding_at=time.time()+18000)
            for n in range(4):self.assertIsNotNone(paper.record(dict(item,symbol=f'COIN{n}USDT'),path))
            self.assertIsNone(paper.record(dict(item,symbol='FIFTHUSDT'),path))
            with paper.session(path) as db:
                self.assertEqual(virtual.used(db),D(240))
                self.assertEqual(db.execute('SELECT target_usdt FROM episodes LIMIT 1').fetchone()[0],'30')
                self.assertEqual(db.execute('SELECT COUNT(*) FROM checkpoints').fetchone()[0],36)
                db.execute("UPDATE virtual_state SET state='closed' WHERE episode_id=1")
                self.assertEqual(virtual.used(db),D(180))
            self.assertIsNotNone(paper.record(dict(item,symbol='FIFTHUSDT'),path))
