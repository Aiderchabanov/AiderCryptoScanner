# Crypto Spread Scanner — Binance ↔ Gate

The Telegram service estimates a repeatable $50 spot arbitrage cycle with $50 reserved on each exchange: buy a coin, withdraw it to the other exchange, sell it, and return USDT to the buying exchange. It alerts only when estimated net profit after costs and a further 0.20% price-change buffer is at least `MIN_NET_PROFIT_PCT` (default 0.5%). It never places orders or withdraws funds. The budget is fixed at $50 in code so an old Render `TRADE_USDT=1000` setting cannot increase it.

It consumes current buy and sell order books in both directions, account-specific spot taker rates, active matching coin and USDT networks, chain-specific fixed and percentage withdrawal costs, and Gate deposit fees reported by its API. It also checks active market pairs, their minimum order quantity and notional value, maximum market size, and the quantity step on the selling exchange. Any unsellable fractional remainder is excluded from profit. Order-book slippage is included in execution prices and shown separately without deducting it twice. If depth, pair limits, compatible active networks, fees, or account API access are missing, the candidate is unverified and no trade-ready alert is sent. Network information and pair rules are cached for at most 30 seconds and trading rates for 60 seconds; an API failure after expiry never falls back to an older value. An alert remains an estimate: prices and transfer availability may change before execution, potentially by more than the 0.20% buffer. Actual balances, taxes, promotions paid in other assets, addresses and memo/tags are not verified.

## Render environment

On the active `crypto-spread-scanner-eu` Render service, keep the existing `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Add these **secret environment variables** yourself; never commit or send them in chat:

| Name | Purpose |
| --- | --- |
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | Read-only account trade fees and network configuration |
| `GATE_API_KEY` / `GATE_API_SECRET` | Read-only personal spot fee and withdrawal status |

Create keys on the same accounts you intend to use. Gate's APIv4 key needs **wallet read-only** access for the fee and withdrawal status calls. Leave trading, fund transfer, and withdrawal write permissions disabled. Check the account's exact allowed permissions. Gate's IP allowlist accepts individual IPv4 addresses, not CIDR ranges; the shared Render `/24` ranges cannot be entered. For an IP-restricted key, use a dedicated outbound IPv4 address rather than choosing one arbitrary IP in Render's ranges.

The service sends no signals until all four values exist. `GET /` exposes `cost_data_ready` without secrets. That flag only checks presence; invalid permissions cause the scanner to skip opportunities. Old `KUCOIN_*`, `BYBIT_*`, and `MEXC_*` environment values are unused and can be removed after the new keys are running. Do not change the Telegram webhook used by other services.

Optional settings: `MIN_NET_PROFIT_PCT=0.5`, `SCAN_INTERVAL_SEC=20`, `ALERT_COOLDOWN_SEC=1800`, `MAX_CANDIDATES_PER_SCAN=10`. `TRADE_USDT` is retained in the blueprint for clarity but the spot scanner uses a fixed $50 budget. The scanner shortlists by gross spread, then checks up to ten candidates with full costs each scan. A sleeping Render Free service does not scan. HTTP 418/429 and 401/403 trigger a pause for the affected host or endpoint; the scanner never substitutes an invented fee.

## Tests

`python -m unittest discover -v`
