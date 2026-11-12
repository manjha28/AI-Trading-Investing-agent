# Valuation scoring — methodology (PROVISIONAL, ACTIVE as an optional component since research-score-0.2.0)

Status: **active but provisional.** `valuation_scoring.py` feeds the `valuation` Research component (weights in section 4, now applied). The component is OPTIONAL: while it is blocked its weight is excluded from the denominator, so scores, confidence and gates equal model 0.1.0. Evidence is provider-first (reported value used as-is; calculated only when no valid current reported value exists; Dhan P/E, EPS, EBITDA, ROE and benchmarks never used). Peers are compared only on the same origin/provider basis. Old text below is retained for the reasoning. Every weight and threshold below is a hypothesis, not a validated value.
Valuation Intelligence (the metrics, `app/services/valuation.py`) is live and display-only.

## 1. What exists today

| Layer | State |
|---|---|
| Valuation metrics (market cap, P/E TTM, P/B, BVPS, P/S, PEG, dividend yield, ROCE, interest coverage; EV, EV/EBITDA, Net debt/EBITDA) | Live, display-only, stored in each brief (`sections.valuation`) |
| Peer comparison (P/E, P/B, P/S) | Live, shown only with ≥ 5 comparable peers |
| Valuation score | Shadow module + tests only. **Not in any horizon weight.** |
| Production weights (`research-score-0.1.0-provisional`) | Unchanged |

## 2. Data requirements and today's gaps (measured on the dev database, 23 stored equity rows)

* **Current price** — required for every price-based multiple. 12 of 23 rows had a stale or missing price.
* **Shares outstanding** — stored for ~70% of statement rows; stale/placeholder values are rejected. A price × shares vs provider market-cap check (>10% apart) blocks ratios when the share count is wrong (caught EASEMYTRIP in the sample).
* **TTM earnings** — needs 4 consecutive quarters; PEG needs 8. Yahoo gives ~5 quarters, so **PEG is unavailable for all 23**.
* **Cash and equivalents** — stored by no provider. **EV, EV/EBITDA, Net debt/EBITDA are unavailable for all 23** until a licensed feed or a new statement field supplies cash (and EBITDA).
* **Peers** — tracked universe is ~13 instruments; **no industry has even one peer**, so every comparison is `insufficient_peers`.
* **Sector/industry** — present only for instruments whose provider snapshot has it (5 of 13).
* Dividend yield units differ by provider (Yahoo percent, Dhan fraction); normalised per source, not independently verifiable.

Consequence: **the shadow score is blocked for 23 of 23** today. That is the correct behaviour; the methodology cannot be activated until the peer universe and cash/EBITDA data exist.

## 3. Method (shadow module)

1. **Sector family** from the provider sector/industry text: `financials`, `asset_heavy_cyclical`, `growth_asset_light`, `default`.
2. **Metrics per family, grouped by independent evidence** — P/E and PEG share one *earnings* group so correlated metrics cannot double count:

| Family | earnings (P/E, PEG) | book (P/B) | sales (P/S) | enterprise (EV/EBITDA) |
|---|---|---|---|---|
| financials | 0.50 | 0.50 | – | – |
| asset_heavy_cyclical | 0.30 | 0.35 | – | 0.35 |
| growth_asset_light | 0.55 | 0.20 | 0.25 | – |
| default | 0.45 | 0.30 | 0.25 | – |

3. **Metric sub-score** = `100 − percentile rank among peers` (peers with a *lower* multiple make the stock look dearer). No universal cutoffs.
4. **Group score** = mean of its scored metrics; **valuation score** = weighted mean of scored groups (renormalised over scored groups only).
5. **Labels (provisional):** ≥ 65 attractive, < 35 expensive, otherwise fair.
6. **Not scored — never zero or neutral:** negative/zero earnings, non-positive equity, missing or `not_meaningful` metrics, stale/absent price, fewer than 5 peers.
7. **Blocked** unless ≥ 2 independent groups are scored **and** ≥ 60% of the family's group weight is covered.
8. **Confidence** (separate from score) = 0.6 × weight coverage + 0.4 × peer depth (min peers / 15).
9. **Market capitalisation** is context only and is never scored.

## 4. Proposed integration (NOT applied)

Add `valuation` as a fifth component. Starting hypothesis — take the weight proportionally from the existing four so each horizon still sums to 1:

| Horizon | valuation | rescale of existing four |
|---|---|---|
| swing | 0.05 | × 0.95 |
| positional | 0.15 | × 0.85 |
| long_term | 0.20 | × 0.80 |

Rules on activation: a new model version (`research-score-0.2.0-provisional`) that stores its weights; old assessments keep the version and weights they were produced with and are **never recomputed**; a blocked valuation component is excluded and the remaining weights renormalise exactly as other blocked components do today; valuation must not unblock or upgrade a recommendation by itself (an `attractive` label cannot override the existing risk gates).

## 5. Validation protocol (required before activation)

1. **Calculation review** — independent re-computation of every metric for a stratified sample (≥ 5 companies per family, including a bank, a loss-maker, a company with a recent split/bonus, a standalone-only filer) against the company's filings. Done so far: computed P/E and P/B agree with provider figures within ~5% for the four stocks with fresh data (see report); this is a sanity check, not a review.
2. **Coverage review** — fraction of the NSE/BSE research universe for which each metric and the score is computable. Gate: ≥ 70% of universe scored per family.
3. **Peer-universe** — requires fundamentals and prices for a full sector universe, which the app does not hold today (needs the licensed feed / bulk price history).
4. **Historical outcome test** — every research brief already stores `sections.valuation` and the model version. Starting now, snapshots accumulate; evaluate forward 3/6/12-month returns of `attractive` vs `expensive` labels within sector, after ≥ 200 labelled observations per family. A retrospective test is **not possible today**: no historical price × shares × TTM snapshots exist and the database holds ~3 years of statements for 12 companies.
5. **Pre-registered acceptance** — rank-IC and top-vs-bottom tercile spread defined before looking at results; thresholds and weights frozen during the test.
6. **Sign-off** — a reviewer other than the implementer approves calculations, coverage and results before the weight leaves shadow mode.

## 6. Known limitations

Peer multiples are computed from tracked instruments, not index-wide medians; PEG is trailing; dividend yield is provider-reported; EV needs cash that no provider supplies; sector classification is keyword-based on provider text.
