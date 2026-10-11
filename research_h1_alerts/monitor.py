"""Research-only H1 >=2 causally known level breakouts. No trading APIs or orders."""
import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = "https://data-api.binance.vision/api/v3"
TG = "https://api.telegram.org"
SYMBOLS = """AAVE ADA APT ARB AVAX BCH BNB BTC DOGE DOT ETH FET FIL HBAR ICP INJ LINK LTC NEAR ONDO RENDER SOL SUI TRX UNI WLD XLM XRP ATOM ALGO ETC OP SEI TIA PEPE SHIB TON ENA JUP WIF BONK PENDLE CRV GRT SAND AXS LDO RUNE KAS ZEC""".split()
STATE = os.environ.get("RESEARCH_STATE_FILE", "")
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
DRY_RUN = "--dry-run" in sys.argv
LOG = logging.getLogger("research.h1")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def utc(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="minutes")


def api(path, **params):
    r = requests.get(BASE + path, params=params, timeout=18)
    # Never bypass Binance blocks, limits or Retry-After.
    if r.status_code in (403, 418, 429, 451):
        raise RuntimeError("Binance HTTP %s; no retry bypass" % r.status_code)
    r.raise_for_status()
    return r.json()


def atr14(bars):
    value = None
    result = []
    for i, c in enumerate(bars):
        prev = bars[i-1]["c"] if i else c["c"]
        tr = max(c["h"]-c["l"], abs(c["h"]-prev), abs(c["l"]-prev))
        value = tr if value is None else ((value * min(i, 13)) + tr) / (min(i, 13) + 1)
        result.append(value)
    return result


def pivots_at(bars, i):
    """A swing at j=i-2 becomes known only after bar i closes."""
    j = i-2
    if j < 2:
        return []
    out = []
    v = bars[j]
    if v["h"] > max(bars[j-1]["h"], bars[j-2]["h"]) and v["h"] >= max(bars[j+1]["h"], bars[j+2]["h"]):
        out.append({"dir":"up", "price":v["h"], "known":i})
    if v["l"] < min(bars[j-1]["l"], bars[j-2]["l"]) and v["l"] <= min(bars[j+1]["l"], bars[j+2]["l"]):
        out.append({"dir":"down", "price":v["l"], "known":i})
    return out


def candidates(symbol, bars):
    """Conservative: distinct closes across 2 previously known levels on separate H1 bars."""
    if len(bars) < 180:
        return []
    av = atr14(bars)
    prior = []
    ev = []
    for i in range(20, len(bars)):
        prior.extend(pivots_at(bars, i))
        for direction in ("up", "down"):
            elig = [p for p in prior if p["dir"] == direction and p["known"] < i and p["known"] >= i-144 and
                    ((bars[i-1]["c"] <= p["price"] < bars[i]["c"]) if direction == "up" else
                     (bars[i-1]["c"] >= p["price"] > bars[i]["c"]))]
            elig.sort(key=lambda p:p["price"], reverse=direction=="down")
            kept = []
            for p in elig:
                if all(abs(p["price"]-q["price"]) >= 0.25*av[i-1] for q in kept):
                    kept.append(p)
            for p in kept:
                ev.append({"dir":direction, "p":p["price"], "bar":i})
    out = []
    last = len(bars)-1
    for direction in ("up", "down"):
        # Fresh signal only: second break must be on the last closed H1 candle.
        e = [x for x in ev if x["dir"] == direction and x["bar"] >= last-24]
        for k, first in enumerate(e):
            second = next((z for z in e[k+1:] if z["bar"] == last and z["bar"] > first["bar"] and
                           (z["p"] > first["p"] if direction == "up" else z["p"] < first["p"]) and
                           abs(z["p"]-first["p"]) >= 0.25*av[z["bar"]-1]), None)
            if second is None:
                continue
            boundary = max(first["p"], second["p"]) if direction=="up" else min(first["p"], second["p"])
            if not (bars[last]["c"] > boundary if direction=="up" else bars[last]["c"] < boundary):
                continue
            # Reject significant counter-movement from peak to subsequent low (or reverse).
            extreme = bars[first["bar"]]["h"] if direction=="up" else bars[first["bar"]]["l"]
            draw = 0
            for b in bars[first["bar"]+1:]:
                if direction=="up":
                    draw = max(draw, extreme - b["l"])
                    extreme = max(extreme, b["h"])
                else:
                    draw = max(draw, b["h"] - extreme)
                    extreme = min(extreme, b["l"])
            if draw >= 0.5*av[first["bar"]-1]:
                continue
            payload = {"pair":symbol, "direction":direction, "levels":[first["p"],second["p"]],
                       "level2_utc":utc(bars[second["bar"]]["t"]),
                       "close_utc":utc(bars[last]["close"]),
                       "close":bars[last]["c"], "boundary":boundary}
            payload["id"] = hashlib.sha256(json.dumps(
                [symbol,direction,first["p"],second["p"],bars[second["bar"]]["t"]],
                separators=(",",":")).encode()).hexdigest()[:24]
            out.append(payload)
    return out


def load_state():
    if not STATE:
        raise RuntimeError("RESEARCH_STATE_FILE not set: durable dedup state required")
    p=Path(STATE)
    if not p.is_file():
        raise RuntimeError("Durable state file absent; initialize separately before enabling Telegram")
    obj=json.loads(p.read_text())
    if not isinstance(obj.get("sent_ids"), list):
        raise RuntimeError("Malformed state file: fail closed")
    return set(obj["sent_ids"])


def persist(ids):
    p=Path(STATE)
    tmp=p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"sent_ids":sorted(ids)},sort_keys=True))
    os.replace(tmp,p)


def message(c, latest_quote):
    side="СОПРОТИВЛЕНИЯ ↑" if c["direction"]=="up" else "ПОДДЕРЖКИ ↓"
    pair=c["pair"]
    v=latest_quote
    return ("🔎 H1: ДВА УРОВНЯ ПРОБИТЫ\n"
            + pair + " | " + side + "\n"
            + "Уровни: " + " → ".join(format(z,".9g") for z in c["levels"]) + "\n"
            + "Close H1: " + format(c["close"],".9g") + " USDT (" + c["close_utc"] + " UTC)\n"
            + "Текущая " + v["kind"] + ": " + format(v["price"],".9g") + " USDT (проверено " + v["utc"] + " UTC)\n"
            + "За последним уровнем: %.3f%%\n" % (100*abs(v["price"]-c["boundary"])/c["boundary"])
            + "Второй пробой: " + c["level2_utc"] + " UTC\n"
            + "Исследовательское событие, не сигнал входа.\n"
            + "https://www.binance.com/en/trade/" + pair.replace("USDT","_USDT") + "?type=spot")


def scan_once():
    now=api("/time")["serverTime"]
    info=api("/exchangeInfo")
    active={x["symbol"] for x in info["symbols"] if x["quoteAsset"]=="USDT"
            and x["status"]=="TRADING" and x.get("isSpotTradingAllowed",False)}
    # Fail closed before any market requests capable of triggering messages.
    sent=set() if DRY_RUN else load_state()
    if not DRY_RUN and (not TOKEN or not CHAT):
        raise RuntimeError("Telegram configuration unavailable; no sends")
    for root in SYMBOLS:
        symbol=root+"USDT"
        if symbol not in active:
            LOG.warning("%s unavailable / non-TRADING Spot",symbol)
            continue
        try:
            data=api("/klines",symbol=symbol,interval="1h",limit=300)
            bars=[{"t":x[0],"o":float(x[1]),"h":float(x[2]),"l":float(x[3]),
                   "c":float(x[4]),"close":x[6]} for x in data if x[6] < now]
            if any(b["h"] < max(b["o"],b["c"],b["l"]) or b["l"] > min(b["o"],b["c"],b["h"]) or
                   b["t"]+3599999 != b["close"] for b in bars) or any(
                       bars[i]["t"]-bars[i-1]["t"]!=3600000 for i in range(1,len(bars))):
                LOG.warning("%s INVALID_OHLCV_OR_GAP",symbol)
                continue
            if not bars or now-bars[-1]["close"] > 2*3600*1000:
                LOG.warning("%s STALE_DATA",symbol)
                continue
            found=candidates(symbol,bars)
            if not found:
                continue
            book=api("/ticker/bookTicker",symbol=symbol)
            # Live BID for upward signals, live ASK for downward ones, never confused with H1 close.
            for c in found:
                q=float(book["bidPrice"] if c["direction"]=="up" else book["askPrice"])
                valid=q>c["boundary"] if c["direction"]=="up" else q<c["boundary"]
                if not valid or c["id"] in sent:
                    continue
                m=message(c,{"kind":"Bid" if c["direction"]=="up" else "Ask",
                             "price":q,"utc":utc(api("/time")["serverTime"])})
                if DRY_RUN:
                    LOG.info("DRY RUN candidate %s %s",symbol,c["id"])
                    print(m)
                    continue
                if not TOKEN or not CHAT:
                    raise RuntimeError("Telegram env missing; no send")
                response=requests.post(TG+"/bot"+TOKEN+"/sendMessage",
                                       json={"chat_id":CHAT,"text":m,"disable_web_page_preview":True},timeout=15)
                response.raise_for_status()
                if not response.json().get("ok"):
                    raise RuntimeError("Telegram send not confirmed")
                sent.add(c["id"])
                persist(sent)
                LOG.info("Telegram delivery accepted: %s %s",symbol,c["id"])
        except (ValueError,KeyError,requests.RequestException,RuntimeError) as exc:
            LOG.error("%s scan failure: %s",symbol,type(exc).__name__)
            if isinstance(exc, RuntimeError) and "Binance HTTP " in str(exc):
                raise  # stop scan on rate/geo restriction
    return 0


if __name__=="__main__":
    sys.exit(scan_once())
