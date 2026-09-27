# Crypto Spread Scanner — Bybit ↔ MEXC

Telegram scanner estimates a repeatable spot arbitrage cycle for `TRADE_USDT` (default $1,000): buy the coin, withdraw it to the other exchange, sell it, and return USDT to the buying exchange. It sends an alert only when estimated net profit is at least `MIN_NET_PROFIT_PCT` (default 0.5%). It never places orders or withdraws funds.

The estimate uses the account's actual taker fee for each symbol, current bid/ask orderbook depth (up to 200 levels), active matching coin and USDT deposit/withdraw networks, fixed and percentage withdrawal fees where available, and the slippage inherent in consuming the depth. If any required cost or liquidity is unavailable, no alert is sent. Network data is cached for 5 minutes and trade fees for 1 hour. Prices and network status can change before execution; an alert is an estimate, not guaranteed profit. Taxes, deposit fees not exposed by these APIs, exchange-specific promotions paid in other assets, and price movement during transfer are outside the model.

## Render environment

Keep existing `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Add these as **secret environment variables** in Render; never commit them or send them in chat:

| Name | Purpose |
| --- | --- |
| `BYBIT_API_KEY` / `BYBIT_API_SECRET` | HMAC key with **read-only** account fee and asset/chain information |
| `MEXC_API_KEY` / `MEXC_API_SECRET` | Key with **SPOT_ACCOUNT_READ** and **SPOT_WITHDRAW_READ**, no trade or withdrawal write access |

The service does not send signals until all four values are configured. `GET /` exposes `cost_data_ready` without exposing secrets. A ready value only says the values exist; invalid key permissions are handled by skipping the signal. On both exchanges, create keys scoped to the account whose fees you want to model. Do not grant order placement or withdrawal rights.

Optional settings: `MIN_NET_PROFIT_PCT=0.5`, `TRADE_USDT=1000`, `SCAN_INTERVAL_SEC=20`, `ALERT_COOLDOWN_SEC=1800`, `MAX_CANDIDATES_PER_SCAN=10`. The scanner first shortlists by gross spread, then checks up to 10 candidates with full costs per scan. The old `MIN_SPREAD_PCT`, `BYBIT_FEE_PCT`, `MEXC_FEE_PCT`, and `MIN_TOP_BOOK_USDT` variables are no longer used; they can stay in Render until the user removes them.

`/start` in Telegram continues to respond. The scanner sleeps with Render Free unless an external scheduled wake request keeps it active.

## Tests

`python -m unittest discover -v`
