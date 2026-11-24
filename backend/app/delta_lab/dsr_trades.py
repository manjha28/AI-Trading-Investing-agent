"""Deflated Sharpe ratio on TRADE-level returns (round 3). Method:
 - the return of a trade = its net P&L in bps of the notional it carried;
 - every round-2 variant (R1-R5, both symbols) is run over the full sample under baseline costs; its per-trade Sharpe (mean / std of trade returns) is one trial;
   the variance V of those Sharpes across trials with at least 30 trades is the 'variance across variants';
 - the benchmark SR0 is the expected best of N zero-edge trials: SR0 = sqrt(V) * ((1-g) Z(1-1/N) + g Z(1-1/(N e))), g = Euler-Mascheroni, with N = the true variant count (148, including
   round 1's 56 and the 18 tick variants, which share this V);
 - DSR = Z( (SR_hat - SR0) sqrt(T-1) / sqrt(1 - skew SR_hat + (kurt-1)/4 SR_hat^2) ), T = number of trades of the best holdout result.
Output: reports/round3_dsr.json. Hypothetical research; decision support, not investment advice."""
from __future__ import annotations

import json
import math
from statistics import NormalDist

import numpy as np

from . import round2 as r2
from .engine import simulate
from .round2_run import BASE, CFG
from .run import REPORTS

N_TRUE = 148
Z = NormalDist()


def trade_returns(trades: list[dict]) -> np.ndarray:
    return np.array([t["net"] / t["notional"] * 1e4 for t in trades if t["notional"]])


def dsr(returns: np.ndarray, n: int, v: float) -> dict:
    T = len(returns)
    sr = returns.mean() / returns.std(ddof=1)
    g = 0.5772156649
    sr0 = math.sqrt(v) * ((1 - g) * Z.inv_cdf(1 - 1 / n) + g * Z.inv_cdf(1 - 1 / (n * math.e)))
    c = returns - returns.mean()
    skew, kurt = float((c ** 3).mean() / returns.std() ** 3), float((c ** 4).mean() / returns.std() ** 4)
    denom = math.sqrt(max(1e-9, 1 - skew * sr + (kurt - 1) / 4 * sr ** 2))
    return {"trades": T, "sharpe_per_trade": float(sr), "benchmark_sr0": float(sr0), "skew": skew, "kurtosis": kurt, "deflated_sharpe_prob": float(Z.cdf((sr - sr0) * math.sqrt(T - 1) / denom)),
            "plain_p_value_single_test": float(1 - Z.cdf(sr * math.sqrt(T)))}


def main() -> None:
    markets = {"BTCUSD": r2.build_binance_market("BTCUSDT", "BTCUSD"), "ETHUSD": r2.build_binance_market("ETHUSDT", "ETHUSD")}
    cuts = {s: int(len(m.t) * 0.75) for s, m in markets.items()}
    srs: list[float] = []
    for fam, grid in r2.GRIDS2.items():
        for g in grid:
            if fam == "R5":
                sig = r2.r5_signals(markets["BTCUSD"], markets["ETHUSD"], *g)
                tr = []
                for s, m in markets.items():
                    tr += simulate(m, sig[s], BASE, CFG)[0]
                subjects = [tr]
            else:
                fn = {"R1": r2.r1, "R2": r2.r2, "R3": r2.r3, "R4": r2.r4}[fam]
                subjects = [simulate(m, fn(m, *g), BASE, CFG)[0] for m in markets.values()]
            for tr in subjects:
                r = trade_returns(tr)
                if len(r) >= 30 and r.std(ddof=1) > 0:
                    srs.append(float(r.mean() / r.std(ddof=1)))
    v = float(np.var(srs, ddof=1))
    best = simulate(markets["BTCUSD"], r2.r3(markets["BTCUSD"], None), BASE, CFG, window=(cuts["BTCUSD"], len(markets["BTCUSD"].t)))[0]
    out = {"method": __doc__, "trials_with_enough_trades": len(srs), "variance_of_sharpe_across_variants": v, "n_variants_true": N_TRUE, "best": "R3 BTCUSD holdout (frozen rules)", **dsr(trade_returns(best), N_TRUE, v)}
    (REPORTS / "round3_dsr.json").write_text(json.dumps(out, indent=1))
    print(json.dumps({k: v for k, v in out.items() if k != "method"}, indent=1))


if __name__ == "__main__":
    main()
