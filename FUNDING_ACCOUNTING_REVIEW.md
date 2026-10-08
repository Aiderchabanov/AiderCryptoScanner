# PAPER funding accounting — prepared, not deployed

Base commit: `6de669c36a31654d5c96ff428311f0adaa88e84f`.

## Confirmed cause

`virtual.quote` formerly used only `time.time() < next_funding_at`.
Before that timestamp it returned zero funding; afterwards it returned NULL
forever. No historical funding collector or settlement evidence journal existed.
`funding_live:<episode>` stores only a current quote, not a historical payment.

Read-only Neon SELECT on project `super-mode-45323575`, branch
`br-shy-sky-b5lob6jc`, database `neondb`, confirmed episodes 26, 27 and 28
remain open with UNKNOWN funding and NULL full Net. No diagnostic code was run
against production; no production records/schema were changed.

## Official sources and remaining blocker

- MEXC: https://mexcdevelop.github.io/apidocs/contract_v1_en/#get-contract-funding-rate-history
  Public history returns settlement time and historical rate. It does not
  provide the fair price used for settlement valuation.
- BingX: https://bingx-api.github.io/docs-v3/#/en/Swap/Market%20Data/Get%20Funding%20Rate%20History
  Public history documents completed settlement time and rate. Live responses
  observed on 2026-10-08 also contain `markPrice`; its meaning as the exact
  settlement valuation is not documented in the retrieved endpoint specification.
- BingX formula:
  https://bingxservice.zendesk.com/hc/en-001/articles/14857605906575--Notice-Perpetual-Futures-Funding-Rate-Mechanism-Explained
  Funding is position quantity × settlement mark price × historical rate;
  positive funding credits a short and negative funding debits it.

An entry price, current mark, candle OHLC or undocumented historical field must
not substitute for a proven settlement price. Exact PAPER funding for #26–28
is therefore **not confirmed by the currently verified sources**. This patch
does not unblock their auto-close. It cannot yet provide fully functional
confirmed settlement recovery for MEXC/BingX; an official valuation definition
and complete-history proof are still required before enabling that capability.

## Prepared changes

- Public GET-only funding history paths, using existing adapter limiters and
  a five-minute cache. No private funding/account/order API, keys or signatures
  are used for these requests. History pagination is bounded.
- Immutable evidence and collection cursors in existing `virtual_meta`, keyed
  by episode and settlement timestamp. Repeated fetch/restart inserts once;
  conflicting evidence fails closed and requires an explicit audit/version.
- Evidence reducer distinguishes pre-first-settlement zero from UNKNOWN and
  requires complete history, matching symbol/source and verified valuation for
  every settlement before calculating confirmed PAPER funding.
- Existing trading Net and fees are preserved; confirmed funding is added once.
  Crossing the first settlement while fetching books invalidates the earlier zero.
- Current MEXC/BingX adapters deliberately cannot mark settlement valuation as
  verified. Other exchanges retain UNKNOWN after settlement until supported.
- Auto-close predicate, fees, admission, sizing, capital and Telegram formats
  remain unchanged. Closed records are not reconstructed.

## Verification and deployment boundary

Offline tests cover pre-first settlement, positive/negative/multiple funding,
missing history/valuation, restart and immutable dedup, public GET transport,
cached coverage, full-Net addition and UNKNOWN blocking auto-close. Confirmed
valuation scenarios use explicit test fixtures, not proof of live API support.

Validation: 323 offline tests passed (306 existing + 17 new); `git diff --check`
passed. GitHub Actions and production deployment were not launched.

No production episodes were created/closed, no historical P&L overwritten and
no Neon migration performed. User confirmation is required before any deploy.
No deploy or Telegram message has been sent.

PAPER / VIRTUAL ONLY. Real orders, transfers and withdrawals: none.
