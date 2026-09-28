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

## Virtual Spot/Futures episodes

The existing spot scanner keeps its $50 position, $50 reserve, network-cost,
orderbook, fee and 0.20% buffer filters. The read-only spot/perpetual scanner
uses a maximum $50 on each leg and another $50 reserve on each exchange. A
new qualified basis signal is saved as a virtual episode before its Telegram
message is sent. Repeated signals for the same coin and direction during one
continuous episode do not increase the sample count. After an observed
spread ≤0.10%, a new episode may start. Otherwise the cooldown is two hours.

The virtual module samples fresh spot asks and futures bids and their depth at
+1, +5, +15, +30 and +60 minutes. It measures matched base quantity, the
same size as the opening virtual position (at most $50 per leg), and stores
raw best-price and executable VWAP spreads. If a checkpoint is missed by more
than one minute, it is explicitly marked `missing`, never reconstructed from
last price or current books. The first **observed checkpoint** ≤0.10% is the
closure time; this is a sampling estimate, not proof of an exact crossing
between checkpoints. An episode with five observed checkpoints and no closure
is `not_closed_60m`; incomplete episodes are excluded from closing-rate
denominators. Statistics report the percentages closed by each checkpoint,
median and mean first-observed closure, and fraction not closed by 60 minutes.
Coin history is displayed in new basis alerts after 30 complete observations;
below that it says `недостаточно истории`. `/stats` shows scanner-wide results
and `/stats HBAR` one coin in the configured Telegram chat.

Funding is fetched for the specific futures contract during evaluation and
again immediately before creating a virtual episode or sending Telegram. Only
a strictly positive current rate with a future settlement time is allowed.
Zero, negative, missing, or failed API responses reject the signal and virtual
episode; nonpositive rates are logged as `REJECTED_NEGATIVE_FUNDING`. Positive
funding is never counted as guaranteed income. A settlement after the horizon
adds no funding cost. Funding can change; projected convergence and exit
slippage remain estimates, not guaranteed trading profits. No order or
withdrawal endpoint is called.

Storage schema (SQLite):

| Table | Contents |
| --- | --- |
| `episodes` | Coin, spot/futures venues and direction, UTC signal time, target $50, actual leg notionals and size, best and depth prices, raw and executable spread, fee/cost snapshot JSON, funding rate and next timestamp, net model, lowest observed spread, first closure checkpoint, status. |
| `checkpoints` | Episode ID, due and actual times, 1/5/15/30/60-minute horizon, observed or missing status, best and depth prices, both spreads, reduction in percentage points and relative percentage, missing-data reason. |

**Render persistence:** Set `PAPER_DB_PATH=/var/data/scanner.sqlite3` only after
attaching a real persistent disk mounted at `/var/data`. The code checks that
the mount exists. Render's Free web service has an ephemeral filesystem and
cannot attach a persistent disk. Until durable storage is available, basis
alerts and virtual tracking are paused; the existing spot cycle continues.
No database secret is needed for this disk-backed SQLite option. A short local
test can set `PAPER_DB_PATH` to a temporary file; it does not prove production
persistence. Backups are still prudent, especially before schema changes.
