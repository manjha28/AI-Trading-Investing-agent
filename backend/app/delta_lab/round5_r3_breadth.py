"""Round 5: the FROZEN R3 rule (paper_config.FROZEN, hash eaea623c741f) on more symbols, no tuning. Breadth test only.

Universe (picked from today's 24 h turnover, which is a survivorship/selection bias stated in the report): Delta India's top 10 USD perpetuals and
Binance's top 20 USDT perpetuals, crypto underlyings only (gold, silver and stock-tracking contracts excluded). Prices are Binance USD-M 1-minute
klines from data.binance.vision (checksum-verified), 2023-01 to yesterday; the Delta contract spec is used where Delta lists the coin, otherwise a
Delta-cost spec (Delta fees and GST, 10x isolated) with a contract value of about 1-10 USD. Mark price = last price (mark klines not downloaded;
only the liquidation check uses it). Costs: the frozen R3 cost model (CostModel defaults). Every symbol except BTC/ETH is out of sample for R3 as a
whole period (the rule was chosen on BTC/ETH only); BTC/ETH are reported on their round-2 holdout (2025-Q1 on) only.
Hypothetical research; decision support, not investment advice. No order path exists here."""
from __future__ import annotations

import json
import math
import sys
import time
from datetime import date, datetime, timezone
from decimal import Decimal as D
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

from . import binance_data as bd
from . import round2 as r2
from .engine import CostModel, Market, simulate
from .paper_config import PARAM_HASH
from .risk import ContractSpec, RiskConfig
from .round2_run import quarter
from .run import REPORTS
from .stats import summarize

OUT = Path(__file__).resolve().parents[3] / "data" / "binance_r5"
START = date(2023, 1, 1)
CFG = RiskConfig(leverage=D(10), mode="paper")
COST = CostModel()
NOT_CRYPTO = {"XAUT", "PAXG", "XAU", "XAG", "SLVON", "NVDAB", "CL", "BZ", "QQQ", "SPCX", "SOXL", "SOXS", "NVDA", "MU", "SNDK", "SKHYNIX", "SKHY", "KORU",
              "SNXX", "TSLA", "AAPL", "COIN", "MSTR", "HOOD", "SPY", "GOOGL", "AMZN", "META", "NATGAS", "COPPER", "PLATINUM"}
EXCLUDE_BINANCE_UNDERLYING = {"COMMODITY", "EQUITY", "INDEX", "TRADFI"}


def universe() -> tuple[list[dict], dict]:
    with httpx.Client(timeout=30) as c:
        products = {p["symbol"]: p for p in c.get("https://api.india.delta.exchange/v2/products", params={"contract_types": "perpetual_futures"}).json()["result"]}
        tickers = c.get("https://api.india.delta.exchange/v2/tickers", params={"contract_types": "perpetual_futures"}).json()["result"]
        info = {s["symbol"]: s for s in c.get("https://fapi.binance.com/fapi/v1/exchangeInfo").json()["symbols"]}
        b24 = c.get("https://fapi.binance.com/fapi/v1/ticker/24hr").json()
    delta = []
    for t in sorted((t for t in tickers if t["symbol"].endswith("USD")), key=lambda t: -float(t.get("turnover_usd") or 0)):
        base = t["symbol"][:-3]
        if base in NOT_CRYPTO:
            continue
        delta.append({"base": base, "delta_symbol": t["symbol"], "binance_symbol": f"{base}USDT", "source": "delta_top10", "turnover_usd_24h": float(t.get("turnover_usd") or 0)})
        if len(delta) == 10:
            break
    binance = []
    for t in sorted((t for t in b24 if t["symbol"].endswith("USDT")), key=lambda t: -float(t["quoteVolume"])):
        s = info.get(t["symbol"])
        base = t["symbol"][:-4]
        if not s or s.get("contractType") != "PERPETUAL" or s.get("status") != "TRADING" or base in NOT_CRYPTO or str(s.get("underlyingType", "COIN")).upper() in EXCLUDE_BINANCE_UNDERLYING:
            continue
        binance.append({"base": base, "delta_symbol": f"{base}USD", "binance_symbol": t["symbol"], "source": "binance_top20", "quote_volume_24h": float(t["quoteVolume"])})
        if len(binance) == 20:
            break
    merged: dict[str, dict] = {}
    for row in delta + binance:
        cur = merged.setdefault(row["binance_symbol"], {**row, "sources": []})
        cur["sources"].append(row["source"])
        cur["on_binance"] = row["binance_symbol"] in info
        cur["delta_product"] = products.get(row["delta_symbol"])
    return list(merged.values()), {"delta_top10": [r["delta_symbol"] for r in delta], "binance_top20": [r["binance_symbol"] for r in binance]}


def load(sym: str) -> tuple[pd.DataFrame, pd.DataFrame] | None:
    OUT.mkdir(parents=True, exist_ok=True)
    kf, ff = OUT / f"{sym}_last_1m.pkl.gz", OUT / f"{sym}_funding.pkl.gz"
    end = date.today()
    with httpx.Client(timeout=120, follow_redirects=True) as c:
        if not kf.exists():
            try:
                df = bd.klines(c, sym, "klines", START, end)
            except ValueError:          # no file at all for this symbol
                return None
            df[["open", "high", "low", "close", "volume"]] = df[["open", "high", "low", "close", "volume"]].astype("float64")
            df.to_pickle(kf)
        if not ff.exists():
            try:
                bd.funding(c, sym, START, end).to_pickle(ff)
            except ValueError:
                pd.DataFrame({"time": pd.Series(dtype="int64"), "rate": pd.Series(dtype="float64")}).to_pickle(ff)
    return pd.read_pickle(kf), pd.read_pickle(ff)


def spec_for(row: dict, median_price: float) -> tuple[ContractSpec, float, float, str]:
    p = row.get("delta_product")
    if p:
        sym = p["symbol"]
        spec = ContractSpec(sym, D(p["contract_value"]), D(p["tick_size"]), mm_pct=D(p["maintenance_margin"]) / 100, im_pct_min=D(p["initial_margin"]) / 100,
                            slope_threshold_notional=D(p.get("max_leverage_notional") or "1000000"), max_leverage=D(1) / (D(p["initial_margin"]) / 100),
                            taker_fee=D(p["taker_commission_rate"]), maker_fee=D(p["maker_commission_rate"]))
        return spec, float(p["tick_size"]), float(p["contract_value"]), "delta_listed"
    cv = 10 ** math.floor(math.log10(max(median_price, 1e-9) / 10)) if median_price > 10 else 1.0
    tick = 10 ** math.floor(math.log10(median_price * 1e-4)) if median_price > 0 else 1e-6
    spec = ContractSpec(row["delta_symbol"], D(str(cv)), D(str(tick)), mm_pct=D("0.005"), im_pct_min=D("0.01"), slope_threshold_notional=D("1000000"), max_leverage=D(100))
    return spec, tick, cv, "delta_cost_proxy"


def market(row: dict, last: pd.DataFrame, fund: pd.DataFrame) -> tuple[Market, str]:
    t = last["time"].to_numpy(np.int64)
    o, h, lo, c, v = (last[k].to_numpy(float) for k in ("open", "high", "low", "close", "volume"))
    spec, tick, cv, kind = spec_for(row, float(np.median(c)))
    funding = {(int(ts) + 30) // 3600 * 3600: float(r) for ts, r in zip(fund["time"], fund["rate"])}
    m = Market(row["delta_symbol"], spec, tick, cv, t, o, h, lo, c, v, o, h, lo, c, funding)
    m.atr_1h = r2._atr_grid(m, "1h")
    return m, kind


def stats(trades: list[dict]) -> dict:
    s = summarize(trades)
    out = {k: s[k] for k in ("trades", "net", "net_bps_per_trade", "gross_bps_per_trade", "profit_factor", "win_rate", "max_drawdown_pct_of_start", "liquidations")}
    for side in ("long", "short"):
        sub = summarize([t for t in trades if t["side"] == side])
        out[side] = {"trades": sub["trades"], "net_bps_per_trade": sub["net_bps_per_trade"], "profit_factor": sub["profit_factor"], "net": sub["net"]}
    q: dict[str, list[float]] = {}
    for t in trades:
        q.setdefault(quarter(t["entry_time"]), []).append(t["bps_net"])
    out["quarters"] = {k: {"trades": len(v), "net_bps_per_trade": float(np.mean(v))} for k, v in sorted(q.items())}
    out["quarters_positive"] = f"{sum(1 for v in q.values() if np.mean(v) > 0)}/{len(q)}"
    return out


def bootstrap(bps: np.ndarray, n: int = 10000, seed: int = 7) -> tuple[float, float]:
    if len(bps) < 2:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    means = rng.choice(bps, size=(n, len(bps)), replace=True).mean(axis=1)
    return float(np.percentile(means, 5)), float(np.percentile(means, 95))


def btc_eth_holdout() -> dict:
    out = {}
    for b, d in (("BTCUSDT", "BTCUSD"), ("ETHUSDT", "ETHUSD")):
        m = r2.build_binance_market(b, d)
        cut = int(len(m.t) * 0.75)
        trades, _rows, counts = simulate(m, r2.r3(m, None), COST, CFG, window=(cut, len(m.t)))
        out[d] = {**stats(trades), "rejections": counts, "from": datetime.fromtimestamp(int(m.t[cut]), tz=timezone.utc).strftime("%Y-%m-%d")}
    return out


def main() -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    logf = (REPORTS / "round5_r3_breadth.log").open("a")

    def log(msg: str) -> None:
        print(msg, flush=True); logf.write(msg + "\n"); logf.flush()
    rows, picked = universe()
    log(f"hash {PARAM_HASH}; universe {json.dumps(picked)}")
    res: dict = {"param_hash": PARAM_HASH, "picked": picked, "generated": datetime.now(timezone.utc).isoformat(), "symbols": {}, "skipped": {}}
    pooled: list[dict] = []
    for row in rows:
        sym = row["binance_symbol"]
        if row["base"] in ("BTC", "ETH"):
            continue                                   # in-sample for R3: reported on the holdout only, below
        if not row["on_binance"]:
            res["skipped"][row["delta_symbol"]] = "not listed on Binance USD-M (no long 1-minute history)"; log(f"skip {sym}: not on Binance"); continue
        t0 = time.time()
        data = load(sym)
        if data is None or len(data[0]) < 24 * 60 * 90:
            res["skipped"][row["delta_symbol"]] = "under 90 days of 1-minute history"; log(f"skip {sym}: short history"); continue
        m, kind = market(row, *data)
        trades, _r, counts = simulate(m, r2.r3(m, None), COST, CFG)
        pooled += trades
        st = stats(trades)
        res["symbols"][row["delta_symbol"]] = {**st, "sources": row["sources"], "spec": kind, "bars": len(m.t), "from": datetime.fromtimestamp(int(m.t[0]), tz=timezone.utc).strftime("%Y-%m-%d"),
                                               "to": datetime.fromtimestamp(int(m.t[-1]), tz=timezone.utc).strftime("%Y-%m-%d"), "rejections": counts}
        log(f"{row['delta_symbol']}: {st['trades']} trades, net {st['net_bps_per_trade'] if st['net_bps_per_trade'] is not None else float('nan'):.1f} bps, PF {st['profit_factor']}, {time.time() - t0:.0f}s")
        (REPORTS / "round5_r3_breadth.json").write_text(json.dumps(res, indent=1, default=str))
    bps = np.array([t["bps_net"] for t in pooled])
    res["pooled"] = {**stats(pooled), "bootstrap_90_net_bps": bootstrap(bps),
                     "long_bootstrap_90": bootstrap(np.array([t["bps_net"] for t in pooled if t["side"] == "long"])),
                     "short_bootstrap_90": bootstrap(np.array([t["bps_net"] for t in pooled if t["side"] == "short"]))}
    res["btc_eth_holdout"] = btc_eth_holdout()
    (REPORTS / "round5_r3_breadth.json").write_text(json.dumps(res, indent=1, default=str))
    log("DONE")


if __name__ == "__main__":
    sys.exit(main())
