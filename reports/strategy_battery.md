# Strategy battery from published research, on Delta data (25 Nov 2026)

*Hypothetical backtests on completed daily bars; kept apart from real P&L. Decision support, not investment advice.*

**Sources:**
- Time-series momentum: Liu & Tsyvinski, *Risks and Returns of Cryptocurrency* (RFS 2021), https://www.nber.org/papers/w24877
- Cross-sectional momentum: Liu, Tsyvinski & Wu, *Common Risk Factors in Cryptocurrency*, https://cowles.yale.edu/sites/default/files/2022-10/LiuTsyvinskiWu2019%20COMMON%20RISK%20FACTORS.pdf
- Calendar effects: Baur et al. 2019, https://ideas.repec.org/a/eee/finlet/v31y2019icp78-92.html ; NYDIG 2021, https://nydig.com/research/the-temporalities-of-bitcoin-returns
- Funding carry: https://arxiv.org/pdf/2212.06888 and https://arxiv.org/pdf/2510.14435
- Classic systems: MA cross, Donchian/Turtle, Connors RSI(2), Larry Williams volatility breakout, Supertrend, Bollinger squeeze.

**Setup:**
- **Universe:** BTC, ETH, SOL and XRP equal-weighted for the single-asset systems; for the cross-sectional ones, the 20 most-traded Delta coins each day.
- **Portfolio:** daily, 1x gross exposure.
- **Costs:** Delta fees + GST on every unit traded, slippage of 2 bps (majors) or 10 bps (top-20), and Delta funding.
- **Splits:** train to 2025-06-26, validation to 2026-03-11, holdout after (about 8.5 months, mostly a falling market). 40 variants were added to the running total.

| Family | Best on validation | Train Sharpe / ann. | Validation Sharpe / ann. | **Holdout Sharpe / ann. / max DD** |
|---|---|---|---|---|
| Time-series momentum | 56-day, long-short | -0.61 / -36% | 1.15 / +63% | **1.63 / +83% / 14%** |
| Supertrend | 10, x3, long-short | -0.27 / -22% | 0.59 / +19% | **1.58 / +76% / 21%** |
| Bollinger squeeze | 20, 2, exit at middle | -0.99 / -16% | 0.28 / +4% | 0.84 / +15% / 9% |
| Cross-sectional momentum | 28-day, weekly | 1.90 / +203% | 0.13 / -13% | 0.65 / +11% / **64%** |
| Funding carry | 3-day, weekly | 0.24 / +2% | 0.13 / -10% | 0.56 / +10% / 49% |
| **Connors RSI(2)** | 5/95 | **0.98 / +11%** | **0.99 / +16%** | **0.43 / +5% / 8%** |
| Donchian | 55/20, long-short | 0.35 / +6% | 0.72 / +26% | 0.34 / +6% / 19% |
| Calendar | long the weekend | 0.16 / 0% | 0.25 / +3% | 0.17 / +2% / 14% |
| MA crossover | 20/100, long-short | -0.72 / -34% | 1.84 / +148% | -0.33 / -21% / 37% |
| Williams breakout | k = 0.7 | 0.05 / -5% | 1.10 / +34% | -1.08 / -28% / 30% |
| Short-term reversal | 1-day | -2.33 / -86% | 0.47 / +12% | -3.31 / -97% / 92% |

## Reading
- **No family passes in all three periods with a 90% range above zero.** Every holdout daily-return range includes zero.
- **Long-short trend following** (56-day momentum, Supertrend) made +76-83% a year in the holdout because the market fell for most of it. The same rules **lost** in the earlier choppy period, so they depend on the regime, as trend following does everywhere.
- **The most stable is Connors RSI(2) 5/95** (buy deep dips above the 200-day average, short spikes below it, exit at the 5-day average). It was positive in all three periods: Sharpe 0.98 / 0.99 / 0.43, max drawdown 8%. It's small (about +5-16% a year), but it is the only steady one.
- **Cross-sectional momentum** (the academic favourite) was huge in train (+203%), lost in validation, and carries a 64% drawdown on Delta's thin coins. Don't trade it.
- **Next step, if you want it:** paper-test **RSI(2) 5/95** and **56-day momentum long-short** on BTC/ETH/SOL/XRP, frozen, beside R3.

Code: `backend/app/delta_lab/battery.py`; numbers: `reports/delta_runs/battery.json`.
