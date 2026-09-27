# Crypto Spread Scanner — Binance ↔ Gate

The Telegram service estimates a repeatable spot arbitrage cycle for `TRADE_USDT` (default $1,000): buy a coin, withdraw it to the other exchange, sell it, and return USDT to the buying exchange. It alerts only when estimated net profit is at least `MIN_NET_PROFIT_PCT` (default 0.5%). It never places orders or withdraws funds.

It consumes current buy and sell order books in both directions, account-specific spot taker rates, active matching coin and USDT networks, chain-specific fixed and percentage withdrawal costs, and Gate deposit fees reported by its API. If depth, compatible networks, fees, or account API access are missing, no alert is sent. Network information is cached for 5 minutes and trading rates for 1 hour. An alert is an estimate: prices and transfer availability may change before execution. Tax, promotions paid in other assets, and price movement during transfer are outside the model. Check address and memo/tag in the exchanges before any actual transfer.

## Render environment

On the active `crypto-spread-scanner-eu` Render service, keep the existing `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Add these **secret environment variables** yourself; never commit or send them in chat:

| Name | Purpose |
| --- | --- |
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | Read-only account trade fees and network configuration |
| `GATE_API_KEY` / `GATE_API_SECRET` | Read-only personal spot fee and withdrawal status |

Create keys on the same accounts you intend to use. Gate's APIv4 key needs **wallet read-only** access for the fee and withdrawal status calls. Leave trading, fund transfer, and withdrawal write permissions disabled. Check the account's exact allowed permissions. Gate's IP allowlist accepts individual IPv4 addresses, not CIDR ranges; the shared Render `/24` ranges cannot be entered. For an IP-restricted key, use a dedicated outbound IPv4 address rather than choosing one arbitrary IP in Render's ranges.

The service sends no signals until all four values exist. `GET /` exposes `cost_data_ready` without secrets. That flag only checks presence; invalid permissions cause the scanner to skip opportunities. Old `KUCOIN_*`, `BYBIT_*`, and `MEXC_*` environment values are unused and can be removed after the new keys are running. Do not change the Telegram webhook used by other services.

Optional settings: `MIN_NET_PROFIT_PCT=0.5`, `TRADE_USDT=1000`, `SCAN_INTERVAL_SEC=20`, `ALERT_COOLDOWN_SEC=1800`, `MAX_CANDIDATES_PER_SCAN=10`. The scanner shortlists by gross spread, then checks up to ten candidates with full costs each scan. A sleeping Render Free service does not scan.

## Tests

`python -m unittest discover -v`
