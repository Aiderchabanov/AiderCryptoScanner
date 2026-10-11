# H1 multi-level breakout research Telegram notifier

This directory is **research-only** and not integrated with `app.py`, virtual trades, paper balances or order APIs. It has **not been deployed or scheduled**.

## Intended behavior

- Original fixed 50 Binance Spot USDT pairs, hourly **closed** 1H candles, official public market data only.
- Pre-existing swing levels: 2 closed candles to the left and right of each pivot; age ≤144 H1 bars, distinct-level spacing ≥0.25 prior ATR14.
- Require two distinct prior-level breaks on separate H1 closes, in the same direction, current close beyond the last broken level, and no interim retracement ≥0.5 prior ATR14. Check live executable-side quote separately (Bid for up/Ask for down). Research signal, never a trade.
- Messages use the existing Telegram bot through `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` **in the separate research runtime**, not by reading the production scanner's credentials.
- Do not start live sends unless a genuinely durable `RESEARCH_STATE_FILE` exists with initial JSON `{ "sent_ids": [] }`; this avoids repeated notifications. Ephemeral Render free instance filesystem is *not* durable.
- Usage: `python research_h1_alerts/monitor.py --dry-run` to inspect research candidates without Telegram. Production `python research_h1_alerts/monitor.py` requires valid Telegram environment plus a durable, persistent state mount, hourly scheduler and independent testing.

## Blockers to autonomous deployment

1. The existing scanner's secret variable values are intentionally inaccessible; configure them independently in the research environment, never copy their values into GitHub, logs or chat.
2. A persistent storage volume or equivalent is needed for unique signal IDs and crash-restart deduplication. No volume or new database provisioned.
3. A dedicated scheduler/runtime must be set up. An hourly GitHub Action on a non-default branch will not start on schedule; copying workflow onto `main` would affect the production repository and is not authorized.
4. Integration tests for signal sequencing, Telegram send/response and persistent dedup are required before enabling alerts. No delivery has been confirmed.

Source branch: `research/h1-level-alerts-isolated-20261011`. Changes here do not alter the scanner's default branch.