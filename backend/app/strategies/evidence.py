"""Recorded research results per registry strategy (hypothetical backtests on stored real 1-minute Dhan bars). Generated from the validation run; None = not tested."""
from __future__ import annotations

from typing import Any

EVIDENCE: dict[str, dict[str, Any]] = {
 "inside_bar_trend_30m": {
  "symbols": [
   "AXISBANK",
   "BHARTIARTL",
   "HDFCBANK",
   "HINDUNILVR",
   "ICICIBANK",
   "INFY",
   "ITC",
   "KOTAKBANK",
   "RELIANCE",
   "SBIN",
   "TCS"
  ],
  "bars": 1026327,
  "data_window": {
   "start": "2025-11-10T03:45:00+00:00",
   "end": "2026-11-20T09:44:00+00:00"
  },
  "cost_profile": "Dhan \u00b7 NSE \u00b7 Equity Intraday (MIS)",
  "slippage_bps": "2",
  "quantity_note": "10% of a 10 lakh account per trade (backtest sizing)",
  "trades": 413,
  "win_rate": 0.2736,
  "gross_pnl_before_costs": "15153.75",
  "total_charges": "33899.12",
  "total_slippage": "14116.36",
  "net_profit": "-32861.70",
  "profit_factor_net": 0.6088,
  "max_drawdown_pct": -3.306,
  "symbols_profitable": 1,
  "symbols_tested": 11,
  "in_sample": {
   "trades": 316,
   "net_profit": "-20484.14",
   "profit_factor_net": 0.6808,
   "win_rate": 0.288
  },
  "out_of_sample": {
   "trades": 94,
   "net_profit": "-11700.59",
   "profit_factor_net": 0.3953,
   "win_rate": 0.234
  },
  "verdict": "Did not clear costs. Net -32861.70 after charges and slippage; out-of-sample profit factor 0.3953.",
  "note": "Research on stored real 1-minute Dhan bars, hypothetical. Not a recommendation."
 },
 "keltner_chop_breakout_60m": {
  "symbols": [
   "AXISBANK",
   "BHARTIARTL",
   "HDFCBANK",
   "HINDUNILVR",
   "ICICIBANK",
   "INFY",
   "ITC",
   "KOTAKBANK",
   "RELIANCE",
   "SBIN",
   "TCS"
  ],
  "bars": 1026327,
  "data_window": {
   "start": "2025-11-10T03:45:00+00:00",
   "end": "2026-11-20T09:44:00+00:00"
  },
  "cost_profile": "Dhan \u00b7 NSE \u00b7 Equity Intraday (MIS)",
  "slippage_bps": "2",
  "quantity_note": "10% of a 10 lakh account per trade (backtest sizing)",
  "trades": 387,
  "win_rate": 0.2739,
  "gross_pnl_before_costs": "25522.40",
  "total_charges": "31875.32",
  "total_slippage": "15275.65",
  "net_profit": "-21628.52",
  "profit_factor_net": 0.6897,
  "max_drawdown_pct": -2.592,
  "symbols_profitable": 2,
  "symbols_tested": 11,
  "in_sample": {
   "trades": 272,
   "net_profit": "-8452.43",
   "profit_factor_net": 0.8276,
   "win_rate": 0.2868
  },
  "out_of_sample": {
   "trades": 112,
   "net_profit": "-12109.17",
   "profit_factor_net": 0.3895,
   "win_rate": 0.2589
  },
  "verdict": "Did not clear costs. Net -21628.52 after charges and slippage; out-of-sample profit factor 0.3895.",
  "note": "Research on stored real 1-minute Dhan bars, hypothetical. Not a recommendation."
 },
 "prev_day_break_30m": {
  "symbols": [
   "AXISBANK",
   "BHARTIARTL",
   "HDFCBANK",
   "HINDUNILVR",
   "ICICIBANK",
   "INFY",
   "ITC",
   "KOTAKBANK",
   "RELIANCE",
   "SBIN",
   "TCS"
  ],
  "bars": 1026327,
  "data_window": {
   "start": "2025-11-10T03:45:00+00:00",
   "end": "2026-11-20T09:44:00+00:00"
  },
  "cost_profile": "Dhan \u00b7 NSE \u00b7 Equity Intraday (MIS)",
  "slippage_bps": "2",
  "quantity_note": "10% of a 10 lakh account per trade (backtest sizing)",
  "trades": 490,
  "win_rate": 0.251,
  "gross_pnl_before_costs": "28924.45",
  "total_charges": "40311.43",
  "total_slippage": "19339.45",
  "net_profit": "-30726.39",
  "profit_factor_net": 0.7201,
  "max_drawdown_pct": -3.923,
  "symbols_profitable": 2,
  "symbols_tested": 11,
  "in_sample": {
   "trades": 325,
   "net_profit": "-10337.92",
   "profit_factor_net": 0.8546,
   "win_rate": 0.2862
  },
  "out_of_sample": {
   "trades": 157,
   "net_profit": "-17891.70",
   "profit_factor_net": 0.509,
   "win_rate": 0.1975
  },
  "verdict": "Did not clear costs. Net -30726.39 after charges and slippage; out-of-sample profit factor 0.509.",
  "note": "Research on stored real 1-minute Dhan bars, hypothetical. Not a recommendation."
 },
 "five_ema_alert": {
  "symbols": [
   "AXISBANK",
   "BHARTIARTL",
   "HDFCBANK",
   "HINDUNILVR",
   "ICICIBANK",
   "INFY",
   "ITC",
   "KOTAKBANK",
   "RELIANCE",
   "SBIN",
   "TCS"
  ],
  "bars": 1026327,
  "data_window": {
   "start": "2025-11-10T03:45:00+00:00",
   "end": "2026-11-20T09:44:00+00:00"
  },
  "cost_profile": "Dhan \u00b7 NSE \u00b7 Equity Intraday (MIS)",
  "slippage_bps": "2",
  "quantity_note": "10% of a 10 lakh account per trade (backtest sizing)",
  "trades": 2337,
  "win_rate": 0.3107,
  "gross_pnl_before_costs": "21742.45",
  "total_charges": "181597.48",
  "total_slippage": "76303.98",
  "net_profit": "-236158.79",
  "profit_factor_net": 0.571,
  "max_drawdown_pct": -23.616,
  "symbols_profitable": 0,
  "symbols_tested": 11,
  "in_sample": {
   "trades": 1633,
   "net_profit": "-179215.57",
   "profit_factor_net": 0.5583,
   "win_rate": 0.3068
  },
  "out_of_sample": {
   "trades": 703,
   "net_profit": "-66062.20",
   "profit_factor_net": 0.617,
   "win_rate": 0.3215
  },
  "verdict": "Did not clear costs. Net -236158.79 after charges and slippage; out-of-sample profit factor 0.617.",
  "note": "Default (15-minute, sell-only, 1:3). The 5-minute version (6 stocks, same period) was worse: 3844 trades, net -376638.54, profit factor 0.4666. Entry is at the next candle's open after the break, slightly pessimistic versus a live stop order; costs use equity-intraday rates."
 },
 "psar_flip_trend_30m": {
  "symbols": [
   "AXISBANK",
   "BHARTIARTL",
   "HDFCBANK",
   "HINDUNILVR",
   "ICICIBANK",
   "INFY",
   "ITC",
   "KOTAKBANK",
   "RELIANCE",
   "SBIN",
   "TCS"
  ],
  "bars": 1026327,
  "data_window": {
   "start": "2025-11-10T03:45:00+00:00",
   "end": "2026-11-20T09:44:00+00:00"
  },
  "cost_profile": "Dhan \u00b7 NSE \u00b7 Equity Intraday (MIS)",
  "slippage_bps": "2",
  "quantity_note": "10% of a 10 lakh account per trade (backtest sizing)",
  "trades": 686,
  "win_rate": 0.2478,
  "gross_pnl_before_costs": "26003.45",
  "total_charges": "56069.58",
  "total_slippage": "26604.86",
  "net_profit": "-56670.91",
  "profit_factor_net": 0.5857,
  "max_drawdown_pct": -5.912,
  "symbols_profitable": 2,
  "symbols_tested": 11,
  "in_sample": {
   "trades": 458,
   "net_profit": "-30195.49",
   "profit_factor_net": 0.663,
   "win_rate": 0.2642
  },
  "out_of_sample": {
   "trades": 215,
   "net_profit": "-24991.80",
   "profit_factor_net": 0.4551,
   "win_rate": 0.214
  },
  "verdict": "Did not clear costs. Net -56670.91 after charges and slippage; out-of-sample profit factor 0.4551.",
  "note": "Research on stored real 1-minute Dhan bars, hypothetical. Not a recommendation."
 },
 "mfi_dip_30m": {
  "symbols": [
   "AXISBANK",
   "BHARTIARTL",
   "HDFCBANK",
   "HINDUNILVR",
   "ICICIBANK",
   "INFY",
   "ITC",
   "KOTAKBANK",
   "RELIANCE",
   "SBIN",
   "TCS"
  ],
  "bars": 1026327,
  "data_window": {
   "start": "2025-11-10T03:45:00+00:00",
   "end": "2026-11-20T09:44:00+00:00"
  },
  "cost_profile": "Dhan \u00b7 NSE \u00b7 Equity Intraday (MIS)",
  "slippage_bps": "2",
  "quantity_note": "10% of a 10 lakh account per trade (backtest sizing)",
  "trades": 203,
  "win_rate": 0.2857,
  "gross_pnl_before_costs": "12501.70",
  "total_charges": "16735.61",
  "total_slippage": "6865.52",
  "net_profit": "-11099.43",
  "profit_factor_net": 0.6871,
  "max_drawdown_pct": -1.283,
  "symbols_profitable": 2,
  "symbols_tested": 11,
  "in_sample": {
   "trades": 154,
   "net_profit": "-8565.02",
   "profit_factor_net": 0.6943,
   "win_rate": 0.2597
  },
  "out_of_sample": {
   "trades": 44,
   "net_profit": "-3096.33",
   "profit_factor_net": 0.5775,
   "win_rate": 0.3182
  },
  "verdict": "Did not clear costs. Net -11099.43 after charges and slippage; out-of-sample profit factor 0.5775.",
  "note": "Research on stored real 1-minute Dhan bars, hypothetical. Not a recommendation."
 }
}
