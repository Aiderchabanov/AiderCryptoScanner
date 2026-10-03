# BingX: третья биржа, только PAPER / VIRTUAL

Текущий проект расширен отдельными `bingx.py` и `exchanges.py`.
Binance/Gate функции подписи, запросов, стаканов и комиссий сохранены.
Spot → Spot остаётся отключённым по заданию от 03.10; код не удалён.

## Настройки Render Environment

Добавить без изменения прежних переменных:

| Переменная | Значение |
|---|---|
| `BINGX_ENABLED` | `true` для включения; по умолчанию `false` |
| `BINGX_API_KEY` | ключ BingX с правами только чтения |
| `BINGX_API_SECRET` | секрет этого ключа |
| `BINGX_MIN_REQUEST_INTERVAL_SEC` | `1.05` или больше |

Секреты не коммитить, не передавать в чат и не выводить в логи.
До установки ключей рыночные данные можно проверять, но сигналы с BingX
не создаются: персональные торговые комиссии неизвестны.

## Направления

Binance Spot → Gate Futures; Gate Spot → Binance Futures;
Binance Spot → BingX Futures; BingX Spot → Binance Futures;
Gate Spot → BingX Futures; BingX Spot → Gate Futures.
Spot/Futures одной биржи не сравниваются. Ошибка BingX не блокирует Binance/Gate.

Для BingX сохранены обязательные условия: свежий положительный funding,
реальный bid/ask и исполнимые цены по глубине, известные персональные комиссии,
минимальный размер, ликвидность, четыре комиссии, проскальзывание выхода,
защитный резерв 0,20%, модель чистого результата ≥ настроенного порога.
Прежний отдельный эксперимент NEGATIVE Binance/Gate сохранён; для новых
комбинаций с BingX отрицательный spread не обходит порог чистой прибыли.

Позиция до $50 и резерв $50 при виртуальном депозите $100 на каждой бирже.
Для новых эпизодов перед записью проверяется также суммарная занятая сумма
по каждой бирже. Незакрытые старые эпизоды тоже учитываются; `incomplete` и
`not_closed_60m` не освобождают бюджет автоматически. Балансы реальных
бирж не используются и реальные сделки отсутствуют.

## Официальный API

Только `https://open-api.bingx.com`, без переключения хостов при запрете.
Клиент имеет закрытый список GET-эндпоинтов для market data, персональных
комиссий и конфигурации сетей. Нет методов создания ордера (даже test-order),
вывода, перевода, изменения маржи или плеча. Обработка HTTP 401/403/418/429,
Retry-After (секунды или HTTP-date), кода 100410 и ошибок ключей.
Общий ограничитель ≤1 запроса/1,05 секунды включает оба рынка и account API.
Запросы потоков сканирования и наблюдения используют один экземпляр клиента.

Доступны Spot symbols, bid/ask, depth, Perpetual contracts, bid/ask, depth,
funding/nextFundingTime, персональные комиссии, withdrawal/deposit status,
сети, фиксированные комиссии вывода и лимиты. Неизвестные поля остаются
`None` или блокируют обязательную проверку, а не заменяются нулём.
Конфигурация сетей не означает выполненный перевод. В прежней Spot→Futures
модели перевод между биржами не выполняется; withdrawal/deposit fees не
подменяют торговые комиссии. Spot→Spot не включён этим обновлением.

Источники: https://bingx-api.github.io/docs-v3/ и официальные справочники
https://github.com/BingX-API/api-ai-skills/tree/main/skills
(spot-market, spot-trade, swap-market, swap-account, spot-wallet, authentication),
https://bingxservice.zendesk.com/hc/en-001/articles/11258459612175-BingX-Upgrades-API-Rate-Limit-2025-10-16

## Проверки

`python -m unittest discover -v` — изолированные тестовые стаканы, без сети,
без реальных ордеров и без добавления вымышленных данных в рабочую статистику.
`BINGX_ENABLED=true python smoke_bingx.py` — реальные GET, без Telegram и без
записей эпизодов. При установленном ключе проверяет также комиссии и USDT сети.
Числа локального виртуального теста — явно тестовые данные, не живой сигнал.

Для обновления существующего сервиса: загрузить изменения в его репозиторий,
задать только новые BingX переменные, выполнить deploy и проверить логи/root.
Не создавать новый сервис или базу. Neon-схема и `paper_pg.py` не изменены.

---

# Обновление 2026-10-03: Spot → Futures ONLY

- Spot → Spot отключён (`SPOT_SPOT_ENABLED = False`): поток не запускается,
  прямой вызов `scan_once()` не ищет возможности и не отправляет уведомления.
  Старый код сохранён. Данные не удаляются.
- Только Binance Spot → Gate Futures и Gate Spot → Binance Futures, при
  доступности соответствующих рынков. Размер позиции до $50, резерв $50
  на каждой бирже. PAPER / VIRTUAL ONLY.
- POSITIVE: сохранён прежний порог модели результата после расходов ≥0,50%,
  положительный свежий funding и прежние проверки цен/комиссий/ликвидности.
- NEGATIVE: и лучшие bid/ask, и исполнимый spread по стакану должны находиться
  в диапазоне [-2,00%; 0%). Funding строго >0; до следующего начисления ≥4 часа.
  Funding запрашивается повторно непосредственно перед записью и уведомлением.
  Неизвестные обязательные данные блокируют сигнал. Отрицательный сигнал не
  требует положительного результата модели схождения: это отдельный эксперимент
  PAPER, отрицательная оценка честно показана в уведомлении.
- `/stats [монета]`: раздельные POSITIVE / NEGATIVE. Статистика использует только
  `spot_buy/futures_short`, старые записи других направлений сохраняются.
  NEGATIVE: количество, средний и минимальный исполнимый spread входа, число
  наблюдавшихся переходов к ≥0% и >0%; среднее время первого наблюдения ≥0%
  показывается после 30 наблюдавшихся переходов. Фактическое достижение порога
  могло произойти между замерами; точное время не выдумывается.
- Прежние контрольные точки 1/5/15/30/60 мин сохранены. NEGATIVE считается
  достигшим порога только при фактически наблюдавшемся spread ≥0%; POSITIVE
  сохраняет порог ≤0,10%. Это пороги наблюдения, не реализованная прибыль.
  После 60 мин прежнее наблюдение заканчивается; непрерывное сопровождение
  до funding и напоминание за 30 минут до funding в этом архиве не реализованы.
- Gate-подпись, `paper_pg.py`, PostgreSQL-схема и подключения, Binance Retry-After
  сохранены. Нет новых таблиц, удаления истории или восстановления пропущенных
  цен. Реальные ордера, переводы, выводы и автоусреднение отсутствуют.

## Установка обновления

Заменить в существующем репозитории `app.py`, `basis.py`, `paper.py` и тесты
из этого архива. Переменные Render, API-ключи и `PAPER_DATABASE_URL` не менять.
После деплоя проверить `/`: `spot_spot_enabled=false`, `basis_mode=read-only`,
`paper_storage_ready=true`. Затем проверить реальные логи и Telegram.
Локальные тесты не подтверждают состояние работающего Render/Neon.

Локальная проверка: `python -m unittest discover -v`.

---
Ниже сохранена прежняя документация для справки; описание Spot → Spot
относится к отключённому коду.

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

Storage schema (PostgreSQL in production, SQLite for local tests):

| Table | Contents |
| --- | --- |
| `episodes` | Coin, spot/futures venues and direction, UTC signal time, target $50, actual leg notionals and size, best and depth prices, raw and executable spread, fee/cost snapshot JSON, funding rate and next timestamp, net model, lowest observed spread, first closure checkpoint, status. |
| `checkpoints` | Episode ID, due and actual times, 1/5/15/30/60-minute horizon, observed or missing status, best and depth prices, both spreads, reduction in percentage points and relative percentage, missing-data reason. |

**Render persistence:** Create a dedicated free Neon PostgreSQL project and add
its connection string as the secret environment variable `PAPER_DATABASE_URL`
on the existing Render web service. The URL must use PostgreSQL with
`sslmode=require` (or a stricter verification mode). Never put it in GitHub or
chat. The scanner checks connection and schema before enabling paper signals;
unavailable storage fails closed. After restart/deploy, due checkpoints are read
from the same database. A missed one-minute observation window is marked
`missing`, never reconstructed from last trade. Neon free storage and compute
limits apply; monitor usage and back up important history. The SQLite
`PAPER_DB_PATH` option remains for local tests or a real persistent disk, never
Render's ephemeral free filesystem. No API credentials are stored in episodes.

## Persistent virtual lifecycle

`virtual.py` adds only additive Neon tables (`virtual_state`, `virtual_proposals`,
`virtual_events`, `virtual_meta`). Existing episodes and checkpoints remain.
`PAPER_DEPOSIT_USDT` defaults to 500. Used capital is actual Spot cost plus
Futures notional divided by `PAPER_FUTURES_LEVERAGE`. Missing leverage explicitly
logs default 1x; invalid or below-1 leverage blocks new entries. The aggregate
limit is 250 USDT (or 50% of a smaller configured deposit), without a fixed episode
count. Each leg remains capped at 50 USDT. Entry leverage is persisted in existing
virtual metadata; changing the setting affects new entries only. Startup migrates
open allocation records without rewriting entry prices, P&L or closed history.
This is virtual margin accounting, not a liquidation or maintenance-margin model.

Open positions continue after 60 minutes and after restart. Exit prices use Spot
Bid / Futures Ask for the actual entry quantity, including executable depth and
entry/exit fees. Positive entries close at observed spread <=0.10%; negative
entries first have to converge to [0;0.10%], rather than closing immediately on
entry. Funding receipts/payments after settlement are UNKNOWN until an actual
settlement ledger is available: the displayed trading net excludes that unknown
component and is explicitly not claimed as complete Net P&L.

Telegram `/open` shows live P&L and selection buttons; `/close` creates a 60-second
preview followed by a second confirmation. Confirmation fetches fresh prices
again; >0.20 percentage point spread change or >0.20% price change requires a
new preview. Only the configured private CHAT_ID owner can act; group operation
requires `TELEGRAM_OPERATOR_ID`. Strong warnings at +5/+10/+15... pp offer a
manually confirmed additional virtual entry; every entry repeats filters and
shares the same capital cap. Spreads >5% require a second full evaluation that
confirms the anomaly with actual depth. Missing data blocks action.

A persistent update offset, one-use bound confirmation tokens, transaction locks
and unique expansion keys prevent duplicate entries. The persistent notification
outbox claims before sending (at-most-once): an ambiguous network failure is
logged and is not retried automatically, because Telegram sendMessage has no
idempotency key. All notification baselines and warning levels are retained.
Manual closure cannot immediately reopen the same continuous gap.

No exchange order, transfer or withdrawal endpoints are called. Isolated unit
and restart tests use temporary SQLite files; synthetic episodes never enter
Neon. Gate signature, Binance Retry-After and database connection settings stay
unchanged.

### Funding reminder

Every open episode refreshes its actual next funding schedule. On the first
valid observation within 30 minutes before that event, a separate reminder
uses fresh Spot Ask / Futures Bid and executable depth up to $50 per leg.
It displays the actual remaining time in UTC and does not close or add entries.
The episode/event timestamp is a unique persistent outbox key. Delivery is
at-most-once, with a durable claim before HTTP; uncertain Telegram sends are
not retried. Closing is serialized with reminder delivery. Missing live funding
or executable depth blocks the reminder; stale snapshots are never sent.
The original entry funding timestamp remains intact for P&L uncertainty.

## Independent Exchange Risk Monitor

Three separate threads check Binance, Gate and BingX public Spot market/depth,
Futures market/depth and current funding APIs every 120 seconds (minimum 60).
No scanner function is called. Public BingX checks run even when BingX trading
signals are disabled, sharing its existing global pacing lock. If an enabled
BingX read-only key is configured, only allowlisted GET fee endpoints are checked.

Risk state, last successful responses, incident dedup and host cooldowns persist
in additive `risk_checks` / `risk_hosts` Neon tables. Single failures are DEGRADED;
three consecutive failed checks or five minutes of sustained unavailability are
CRITICAL. Recovery resets the incident. Separate alerts identify each exchange
and component. Timeout, 5xx, auth, rate-limit, malformed/missing or stale data
remain distinct statuses. Retry-After seconds and HTTP dates are respected.
The Binance GET helper and its existing bans are reused unchanged. Trading
filters, order handling and virtual lifecycle are unchanged.

Quote freshness is checked against snapshot timestamps when supplied (BingX
depth requires one); endpoints without timestamps can only establish a fresh
HTTP response, not prove the age of the exchange's underlying snapshot.
Telegram delivery remains at-most-once; ambiguous sends are not retried.

### Convergence analytics

Funding >0 remains exclusively an admission filter. Positions are never held
for a funding payment; the continuous observer uses fresh executable bid/ask.
New managed episodes also receive statistics-only 3/6/12/24h checkpoints.
Existing open episodes receive only future checkpoints on recovery, with no
backfill and no Telegram messages caused by checkpoint times. The earlier
1/5/15/30/60m statistics retain their existing denominator.

`virtual_metrics` stores observed maximum spread/expansion, minimum spread,
actual elapsed time at observed convergence, and latest/closing analytics.
Quote JSON includes each leg's trading P&L, entry/exit fee breakdown, entry and
exit slippage, entry funding, known/unknown realized funding and Net P&L.
Spot entry fee is already included in its entry cost and is not deducted twice.
Slippage is included in executable prices, never charged a second time. Old
missing slippage and unknown funding settlement amounts remain null. Metrics
for legacy episodes include known entry values and subsequent real observations,
without claiming to reconstruct earlier peaks. Existing convergence and alert
thresholds are unchanged, including negative-entry handling at zero.

## Full checkpoint valuations

New managed episodes retain their actual entry prices and nine due checkpoints:
1/5/15/30/60 minutes and 3/6/12/24 hours. Fresh books supply both entry-side
spread and executable Spot exit Bid / Futures exit Ask for the matched original
quantity. Additive `virtual_checkpoint_details` stores the P&L snapshot, leg P&L,
fees, slippage, entry/current funding rates and spread change. Unknown current
funding or settlement P&L stays null; a positive rate is not an accrued payment.
Post-close market samples are explicitly hypothetical, not additional realized
profit or an open position. No last-trade prices or Telegram time-point alerts.

The existing sampling window is 60 seconds; a response outside that window
is missing, never backfilled. Old checkpoint prices are not rewritten. Statistics
use the first actually observed convergence recorded by the continuous observer,
not a manual closure. Rates have separate mature cohorts and display sample counts
for each horizon; incomplete non-converged histories and late legacy adoption are
excluded rather than classified as failures. Mean/median are observed convergence
times only, and the 24-hour failure fraction uses the eligible 24-hour cohort.
The existing negative-entry convergence rule [0;0.10%] is preserved.
