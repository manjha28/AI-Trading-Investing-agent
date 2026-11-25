# 5 EMA: the AlgoTest script vs ours, and two orders with the same stop (25 Nov 2026)

*Hypothetical backtest on completed bars; kept apart from real P&L. Decision support, not investment advice. Nothing live was changed.*

## Is our 5 EMA the AlgoTest script? Same idea, three differences
| | AlgoTest Pine script | Ours |
|---|---|---|
| Alert candle | only the candle just before (its low above EMA5) | the latest candle entirely above EMA5, valid until a later candle breaks its low |
| Entry | intrabar, at the alert low (a stop order) | paper test: at the alert low; **live app: at the close of the candle that broke it** |
| Stop / target | alert high / alert low - 3 x alert range | alert high / 3 x (stop - entry) from the fill |
| Other | resets every day | one trade at a time, no daily reset |

## Results (BTC, ETH, SOL, LISTA; 2023 to Nov 2026; Delta fees + GST, slippage; net bps per trade; fixed rules, nothing chosen)
| | 5m train / validation / holdout | 15m train / validation / holdout |
|---|---|---|
| AlgoTest script, exactly | -13.9 / -14.6 / -13.6 (about 21-42k trades) | -15.1 / -14.8 / -11.3 |
| Our live rule (enter at the close) | -14.4 / -16.1 / -14.1 | -14.1 / -14.2 / -13.4 |
| Two orders, same stop and target: close + limit halfway up the alert candle (per signal, both legs added) | -18.3 / -19.9 / -17.8 | -19.5 / -19.1 / -17.7 |

- **The AlgoTest version loses the same as ours**, about 11-15 bps per trade after costs. The small differences don't create an edge.
- **Two orders make it worse:** the second order fills on 43-49% of signals, and each leg loses on its own, so the loss per signal grows from about -14 to about -18 bps. Per contract it sits between the close entry (-14) and the pullback entry (-9), and the money at risk doubles.

Code: `backend/app/delta_lab/five_ema_algotest.py`; numbers: `reports/five_ema_algotest.json`. 4 variants added to the running total.
