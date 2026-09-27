# Crypto Spread Scanner — Binance ↔ KuCoin

The existing Telegram service estimates a repeatable spot arbitrage cycle for `TRADE_USDT` (default $1,000): buy a coin, withdraw it to the other exchange, sell it, then return USDT to the first exchange. It sends an alert only when the estimated net profit is at least `MIN_NET_PROFIT_PCT` (default 0.5%). It never places orders or withdraws funds.

The estimate consumes the current buy and sell order books in both directions, uses account-specific spot taker rates and current active matching withdrawal/deposit networks, applies published fixed and percentage withdrawal fees, and includes the return USDT transfer. When depth, a fee, a compatible network, or API access is missing, it sends no alert. Networks are cached for 5 minutes; account trading rates for 1 hour. Prices and network status may change during transfer. Deposit fees not exposed by these APIs, tax, promotions paid in other assets, and price movement during transfer are outside the model. Check the destination address and memo/tag yourself before any real transfer.

## Render environment

Keep the existing `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Set the following as **secret environment variables** on the active Render service; never send them in chat or commit them:

| Name | Purpose |
| --- | --- |
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | Account trade fees and coin/network configuration; read-only access |
| `KUCOIN_API_KEY` / `KUCOIN_API_SECRET` / `KUCOIN_API_PASSPHRASE` | Actual spot fees and full order book; **General** read-only access |
| `KUCOIN_API_KEY_VERSION` | Key version shown in KuCoin API settings, default `2` |

Create keys for the accounts whose fees you want modeled, with only read permissions. Never allow spot trading or withdrawals on these keys. If an IP allowlist is available, use the service's stable outbound IP; a free host may not have one. If a signed endpoint denies read access, the scanner skips that opportunity.

The service sends no signals until the five credential values exist. `GET /` exposes `cost_data_ready` without exposing credentials. This flag checks presence only; live API permissions are checked during each scan. The old `BYBIT_*` and `MEXC_*` variables are unused and can be removed after the service is running with the new keys.

Optional settings: `MIN_NET_PROFIT_PCT=0.5`, `TRADE_USDT=1000`, `SCAN_INTERVAL_SEC=20`, `ALERT_COOLDOWN_SEC=1800`, `MAX_CANDIDATES_PER_SCAN=10`. The scanner shortlists by gross spread, then checks up to 10 candidates per scan with full costs. `/start` in Telegram responds when polling is available. If another service owns the Telegram webhook, polling is disabled without touching its webhook. A sleeping free Render service does not scan.

## Tests

`python -m unittest discover -v`
