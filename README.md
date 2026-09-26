# Crypto Spread Scanner — Bybit ↔ MEXC

Read-only Telegram alert bot. It does **not** trade and needs no exchange API keys.

## Render environment variables
- `TELEGRAM_BOT_TOKEN` — token from BotFather (secret)
- `TELEGRAM_CHAT_ID` — your Telegram numeric chat ID
- `MIN_SPREAD_PCT=2.0`
- `TRADE_USDT=1000`
- `SCAN_INTERVAL_SEC=20`
- `MIN_TOP_BOOK_USDT=1000`
- `BYBIT_FEE_PCT=0.10`
- `MEXC_FEE_PCT=0.10`

## Important
Signals use executable best ask/bid and top-of-book quantity. Trading fees are estimates configurable by environment variables. Withdrawal/network fees and deposit/withdraw availability are not included in v1, so verify them before any trade.
