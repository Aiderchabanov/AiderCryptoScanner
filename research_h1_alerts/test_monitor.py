"""Offline regression tests. No exchange, Telegram, or trading network access."""
import unittest
from unittest.mock import patch

from research_h1_alerts import monitor as m


def bars():
    out=[]
    closes=[100.0]*180
    closes[-4]=100.0
    closes[-3]=101.0
    closes[-2]=101.0
    closes[-1]=103.0
    for i,c in enumerate(closes):
        out.append({"t":i*3600000,"close":i*3600000+3599999,
                    "o":c,"c":c,"h":c+.05,"l":c-.05})
    return out


class H1Tests(unittest.TestCase):
    def test_pivot_not_knowable_on_current_bar(self):
        b=bars()
        self.assertEqual(m.pivots_at(b,1),[])
        self.assertEqual(m.pivots_at(b,2),[])

    def test_positive_two_consecutive_separate_bar_breakouts(self):
        b=bars()
        last=len(b)-1
        def piv(_bars,i):
            if i==last-4:
                return [{"dir":"up","price":100.5,"known":i}]
            if i==last-2:
                return [{"dir":"up","price":102.0,"known":i}]
            return []
        with patch.object(m,"atr14",return_value=[1.0]*len(b)),patch.object(m,"pivots_at",side_effect=piv):
            signals=m.candidates("BTCUSDT",b)
        self.assertEqual(len(signals),1)
        self.assertEqual(signals[0]["direction"],"up")
        self.assertEqual(signals[0]["levels"],[100.5,102.0])
        self.assertGreater(signals[0]["close"],signals[0]["boundary"])

    def test_same_bar_multiple_levels_is_not_two_confirmations(self):
        b=bars()
        last=len(b)-1
        def piv(_bars,i):
            if i==last-2:
                return [{"dir":"up","price":101.5,"known":i},
                        {"dir":"up","price":102.0,"known":i}]
            return []
        with patch.object(m,"atr14",return_value=[1.0]*len(b)),patch.object(m,"pivots_at",side_effect=piv):
            self.assertEqual(m.candidates("BTCUSDT",b),[])

    def test_past_second_break_does_not_alert(self):
        b=bars()
        b[-1]["c"]=b[-2]["c"]
        b[-1]["o"]=b[-1]["c"]
        b[-1]["h"]=b[-1]["c"]+.05
        b[-1]["l"]=b[-1]["c"]-.05
        with patch.object(m,"atr14",return_value=[1.0]*len(b)):
            self.assertEqual(m.candidates("BTCUSDT",b),[])

    def test_github_issue_state_loaded(self):
        with patch.object(m,"STATE",""),patch.object(m,"issue_api",return_value={
            "body": '<!-- H1_RESEARCH_STATE_V1\\n{"sent_ids":["existing"]}\\n-->'
        }):
            self.assertEqual(m.load_state(),{"existing"})

    def test_github_issue_state_malformed_fails_closed(self):
        with patch.object(m,"STATE",""),patch.object(m,"issue_api",return_value={"body":"wrong"}):
            with self.assertRaises(RuntimeError):
                m.load_state()

    def test_github_issue_state_persist(self):
        with patch.object(m,"STATE",""),patch.object(m,"issue_api") as api:
            m.persist({"xyz","abc"})
            api.assert_called_once()
            self.assertEqual(api.call_args.args[0],"PATCH")
            self.assertIn('"sent_ids": ["abc", "xyz"]',api.call_args.args[1]["body"])

    def test_telegram_not_called_without_credentials(self):
        with patch.object(m,"DRY_RUN",False),patch.object(m,"STATE","/nonexistent/file.json"),patch.object(m.requests,"post") as post:
            with self.assertRaises(RuntimeError):
                m.scan_once()
            post.assert_not_called()


if __name__=="__main__":
    unittest.main()
