# Opt-in PAPER funding model V1 — prepared, not deployed

Continues `fix/paper-funding-evidence-review` from commit
`7019cd8b92e349ed094df652e1d58fbdfe535df2`. No push/deploy or production SQL
writes were performed while preparing this change.

## Activation and legacy isolation

`PAPER_FUNDING_MODE=ESTIMATED` is an explicit opt-in at episode creation.
Default remains CONFIRMED. No production environment variable was changed.
The entry cost snapshot stores mode, method version and activation timestamp
equal to the episode's entry timestamp. Subsequent environment changes cannot
retag existing episodes. No historical backfill or migration of episode data.

Episodes lacking that marker, including production #26–28, retain the existing
strict confirmed-funding quote and auto-close requirements. No manual closure.

## Method fixed before activation

Version: `PAPER_FUNDING_ESTIMATED_V1_PREVIOUS_1M_MARK_CLOSE`.
For each official historical funding timestamp T, choose the **close** of the
exact fully closed minute `[T-60 seconds, T)` from the same Futures venue's
historical mark-price candles (MEXC calls these fair-price candles).

This OHLC close is a MODEL valuation, explicitly NOT an exact exchange
settlement price. No live price, nearest candle, trade-price candle, index price,
entry price or undocumented funding-history markPrice is substituted.
Timestamp not aligned to a minute, missing/duplicate candle or invalid price
produces UNKNOWN. Official zero rate with complete price/history produces zero
MODEL funding; missing data never does.

SHORT model funding in USDT = saved base-asset virtual quantity × model price ×
official historical rate. Positive rate credits the SHORT; negative rate debits
it. Saved episode quantity is already base-asset matched quantity; the contract
multiplier is not applied a second time.

Official public sources:

| Venue | Historical rate | Historical model price |
|---|---|---|
| MEXC | `/api/v1/contract/funding_rate/history` | `/api/v1/contract/kline/fair_price/{symbol}`, Min1 |
| BingX | `/openApi/swap/v2/quote/fundingRate` | `/openApi/swap/v1/market/markPriceKlines`, 1m |
| Binance | `/fapi/v1/fundingRate` | `/fapi/v1/markPriceKlines`, 1m |
| Gate | `/futures/usdt/funding_rate` | `/futures/usdt/candlesticks`, mark_ contract, 1m |

Documentation:
- https://mexcdevelop.github.io/apidocs/contract_v1_en/
- https://bingx-api.github.io/docs-v3/#/en/Swap/Market%20Data/Get%20Funding%20Rate%20History
- https://github.com/BingX-API/api-ai-skills/blob/main/skills/swap-market/api-reference.md
- https://developers.binance.info/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data
- https://www.gate.com/docs/developers/apiv4/en/futures/

## Persistence and completeness

One new additive table, `paper_funding_model_events`, keyed by
`(episode_id, settlement_ms)`. Payload includes venue/symbol, quantity, rate,
timestamps, public source identifiers, receipt times, model candle timestamps,
model price, signed amount, status and method version. No credentials.

UNKNOWN rows may be enriched when official data recovers, retaining previous
UNKNOWN payload and an audit reason. ESTIMATED rows are frozen: restart reuses
their saved model price and amount. Conflicting historical evidence blocks the
model result rather than overwriting an ESTIMATED receipt. Repeated failures
do not create duplicate rows or grow audit history indefinitely.

History is requested from the entry's first scheduled funding through the check
time, with bounded pagination. Completeness is required; first settlement,
previously recorded events and every due independently observed next-funding
checkpoint must be present. No fixed 8-hour cadence is invented. Rates from
current funding quotes are never used for past payments. A provider outage,
pagination exhaustion, stale cached coverage, missing checkpoint or missing
historical rate blocks the full model Net. Completeness relies on the official
history API's complete-range response; an omitted record never observed in a
schedule cannot independently be proven from another invented timetable.

Existing adapter pacing/cooldowns remain in force. Model price is fetched only
for a receipt without a saved successful valuation. No API restriction bypass.

## Net, capital and closure

MODEL_NET = existing Spot leg Net + existing Futures leg Net + sum of all
ESTIMATED funding receipts. Existing leg calculations include actual entry/exit
VWAP depth effects and known trading fees. Those costs are not deducted again;
the entry admission reserve is not converted into another realized fee.

For model episodes only, auto-close requires `abs(spread) <= Decimal("0.10")`
AND known MODEL_NET >= 0. Any UNKNOWN receipt blocks it. A quote crossing the
next settlement boundary is ineligible. The close snapshot retains model Net
and its ESTIMATED provenance separately from confirmed Net/funding.

Confirmed realized P&L and existing capital balances are not credited with
ESTIMATED amounts. Model realized results are saved separately. Closing releases
used capital by the existing OPEN-state accounting; targets, sizes, leverage
and percentage capital limits are unchanged. No balances are reset or rewritten.

Telegram uses existing event timing/dedup and transport. New model episodes say
`Funding: ESTIMATED` / `Model Net P&L` and explicitly state that the result is a
PAPER model, not a confirmed exchange accrual. Missing data says UNKNOWN.

## Compatibility and verification boundary

READ ONLY Neon inspection confirmed episode key BIGINT, TEXT entry JSON and
existing metadata columns. The new table is absent in production and was not
created there. It uses portable PostgreSQL/SQLite types, primary and foreign
keys, and the existing database wrapper. No ALTER/UPDATE of historical episodes
is required. PostgreSQL production DDL was reviewed, not executed.

Live public model-price GET checks succeeded for MEXC, BingX and Gate. Binance
returned HTTP 451 from the diagnostic fetch environment; no bypass was tried.
The Binance adapter is checked with offline fixtures and must return UNKNOWN
if that source is unavailable in the actual runtime.

Tests use temporary local databases/fixtures only. No production episode was
created, updated or closed. No Telegram message was sent. Push, deployment and
production activation require separate user authorization.

Verification: 341 unittest cases passed, including 18 new model-specific cases;
`git diff --check` passed. A repeated READ ONLY Neon query confirmed unchanged
entry snapshot hashes for #26–28 and absence of the new model journal table.

PAPER / VIRTUAL ONLY. No orders, transfers or withdrawals.
