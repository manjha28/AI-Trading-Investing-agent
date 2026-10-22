from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.accounting import PortfolioLedger
from app.api.portfolio_transactions import calculate_portfolio_ledger, get_transactions
from app.config import settings
from app.models import (
    Instrument,
    MarketBar,
    MarketQuote,
    Portfolio,
    PortfolioTransaction,
    PositionThesis,
)
from app.models.alerts import AlertEvent, AlertRule
from app.services.indicators import (
    drawdown_from_trailing_peak as calculate_drawdown,
    pct_change as calculate_pct_change,
    rsi as calculate_rsi,
    sma as calculate_sma,
)
from app.services.price_freshness import classify_quote

PRICE_RULES = {
    "price_above",
    "price_below",
    "pct_move_nd",
    "trailing_drawdown_pct",
    "thesis_target_hit",
    "thesis_stop_hit",
    "unrealized_gain_pct_above",
    "position_weight_above",
    "rsi_threshold",
    "ma_cross",
}
ACTION_IMPLYING_RULES = PRICE_RULES
DAILY_BAR_RULES = {"pct_move_nd", "trailing_drawdown_pct", "rsi_threshold", "ma_cross"}
RULE_TYPES = PRICE_RULES | {"holding_period_crosses_365d", "data_stale"}
RULE_TYPE_PARAMS: dict[str, set[str]] = {
    "price_above": {"price"},
    "price_below": {"price"},
    "pct_move_nd": {"days", "threshold_pct", "direction"},
    "trailing_drawdown_pct": {"days", "threshold_pct"},
    "thesis_target_hit": {"target_index"},
    "thesis_stop_hit": set(),
    "unrealized_gain_pct_above": {"threshold_pct"},
    "position_weight_above": {"threshold_pct"},
    "rsi_threshold": {"period", "threshold", "direction"},
    "ma_cross": {"fast_period", "slow_period", "direction"},
    "holding_period_crosses_365d": set(),
    "data_stale": {"max_age_seconds"},
}


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _bars(db: Session, instrument_id: UUID, limit: int) -> list[MarketBar]:
    latest_source = db.scalar(
        select(MarketBar.source)
        .where(MarketBar.instrument_id == instrument_id, MarketBar.interval == "1d")
        .order_by(MarketBar.timestamp.desc(), MarketBar.retrieved_at.desc())
        .limit(1)
    )
    if latest_source is None:
        return []
    rows = list(
        db.scalars(
            select(MarketBar)
            .where(
                MarketBar.instrument_id == instrument_id,
                MarketBar.interval == "1d",
                MarketBar.source == latest_source,
            )
            .order_by(MarketBar.timestamp.desc())
            .limit(limit)
        )
    )
    return list(reversed(rows))


def _rule_match(
    rule: AlertRule,
    instrument: Instrument,
    quote: MarketQuote | None,
    bars: list[MarketBar],
    ledger: PortfolioLedger | None,
    symbol: str | None,
    thesis: PositionThesis | None,
    now: datetime,
    params: dict[str, Any],
) -> tuple[bool, dict[str, Any], str | None]:
    portfolio_currency = params.get("_portfolio_currency")
    quote_usable, quote_reason, quote_evidence = classify_quote(
        quote, instrument, portfolio_currency, now
    )
    quote_age = quote_evidence["quote_retrieved_age_seconds"]
    price = None
    if quote is not None:
        price = quote.close
    market_inputs: dict[str, Any] = {
        "instrument_id": str(instrument.id),
        "symbol": instrument.symbol,
        "instrument_currency": instrument.currency,
        "portfolio_currency": params.get("_portfolio_currency"),
        "scope": rule.scope,
        "cooldown_seconds": rule.cooldown_seconds,
        "quote_price": str(price) if price is not None else None,
        "quote_source": quote.source if quote else None,
        "quote_timestamp": _utc(quote.timestamp).isoformat() if quote else None,
        "quote_retrieved_at": _utc(quote.retrieved_at).isoformat() if quote else None,
        "quote_age_seconds": quote_age,
        "quote_is_synthetic": quote.is_synthetic if quote else None,
        "quote_is_fresh": quote_evidence["quote_age_within_limit"],
        **quote_evidence,
        "rule_params": rule.params,
    }
    result: bool = False
    if rule.rule_type == "data_stale":
        maximum = int(params["max_age_seconds"])
        reason = (
            "quote_unavailable"
            if quote is None
            else "synthetic_price"
            if quote.is_synthetic
            else "quote_stale"
            if quote_age is None or quote_age > maximum or quote_age < 0
            else None
        )
        return reason is not None, {**market_inputs, "max_age_seconds": maximum}, None

    if rule.rule_type == "holding_period_crosses_365d":
        holding = ledger.positions.get(symbol) if ledger and symbol else None
        start = holding.holding_period_start if holding else None
        if start is not None:
            start = _utc(start)
            crossed_at = start + timedelta(days=365)
            result = start <= now and now >= crossed_at
        else:
            crossed_at = None
        return result, {
            **market_inputs,
            "holding_period_start": start.isoformat() if start else None,
            "crossed_at": crossed_at.isoformat() if crossed_at else None,
            "days_since_crossing": (now - crossed_at).days if result and crossed_at else None,
            "as_of": now.isoformat(),
        }, None

    if rule.portfolio_id is not None:
        if quote_reason == "foreign_currency_no_fx":
            return False, market_inputs, "foreign_currency"

    if rule.rule_type not in DAILY_BAR_RULES:
        if not quote_usable:
            return False, market_inputs, quote_reason

    if rule.rule_type == "price_above":
        threshold = _decimal(params["price"])
        result = price >= threshold
        market_inputs["threshold_price"] = str(threshold)
    elif rule.rule_type == "price_below":
        threshold = _decimal(params["price"])
        result = price <= threshold
        market_inputs["threshold_price"] = str(threshold)
    elif rule.rule_type in DAILY_BAR_RULES:
        required_bars = (
            int(params["days"]) + 1
            if rule.rule_type == "pct_move_nd"
            else int(params["days"])
            if rule.rule_type == "trailing_drawdown_pct"
            else int(params["period"]) + 1
            if rule.rule_type == "rsi_threshold"
            else int(params["slow_period"]) + 1
        )
        if len(bars) < required_bars:
            return False, {
                **market_inputs,
                "bars_used": len(bars),
                "required_bars": required_bars,
            }, "insufficient_history"
        bar_data = [
            {
                "timestamp": _utc(bar.timestamp).isoformat(),
                "close": str(bar.close),
                "source": bar.source,
                "is_synthetic": bar.is_synthetic,
            }
            for bar in bars
        ]
        latest_bar = bars[-1]
        latest_bar_age = (now - _utc(latest_bar.timestamp)).total_seconds()
        daily_bar_max_age = settings.daily_close_price_max_age_seconds
        market_inputs.update(
            price_freshness_basis="daily_bar_close",
            daily_close_price_max_age_seconds=daily_bar_max_age,
            price_freshness_timestamp=_utc(latest_bar.timestamp).isoformat(),
            price_age_seconds=latest_bar_age,
            price_is_fresh=0 <= latest_bar_age <= daily_bar_max_age,
            bars_used=len(bars),
        )
        if any(bar.is_synthetic for bar in bars):
            return False, {**market_inputs, "bars": bar_data}, "synthetic_history"
        if latest_bar_age < 0 or latest_bar_age > daily_bar_max_age:
            return False, {
                **market_inputs, "bars": bar_data,
                "latest_bar_age_seconds": latest_bar_age,
            }, "stale_history"
        if rule.rule_type == "pct_move_nd":
            days = int(params["days"])
            indicator_result = calculate_pct_change(bars, days)
            change = indicator_result.value
            if change is None:
                return False, {**market_inputs, "bars": bar_data}, "insufficient_history"
            origin, latest = bars[-days - 1].close, bars[-1].close
            direction = params["direction"]
            threshold = _decimal(params["threshold_pct"])
            result = change >= threshold if direction == "above" else change <= -threshold
            market_inputs.update(
                days=days, start_close=str(origin), end_close=str(latest),
                pct_change=str(change), direction=direction, threshold_pct=str(threshold),
                indicator_bars_used=indicator_result.bars_used,
                bars=bar_data,
            )
        elif rule.rule_type == "trailing_drawdown_pct":
            days = int(params["days"])
            indicator_result = calculate_drawdown(bars, days)
            drawdown = indicator_result.value
            if drawdown is None:
                return False, {**market_inputs, "bars": bar_data}, "insufficient_history"
            window = bars[-days:]
            peak = max(bar.close for bar in window)
            threshold = _decimal(params["threshold_pct"])
            result = drawdown >= threshold
            market_inputs.update(
                days=days, peak_close=str(peak), end_close=str(window[-1].close),
                drawdown_pct=str(drawdown), threshold_pct=str(threshold),
                indicator_bars_used=indicator_result.bars_used, bars=bar_data,
            )
        elif rule.rule_type == "rsi_threshold":
            period = int(params["period"])
            indicator_result = calculate_rsi(bars, period)
            indicator = indicator_result.value
            if indicator is None or indicator_result.is_synthetic:
                return False, {
                    **market_inputs, "indicator_bars_used": indicator_result.bars_used,
                    "bars": bar_data,
                }, "insufficient_history"
            direction = params["direction"]
            threshold = _decimal(params["threshold"])
            result = indicator >= threshold if direction == "above" else indicator <= threshold
            market_inputs.update(
                period=period, rsi=str(indicator), threshold=str(threshold),
                direction=direction, indicator_bars_used=indicator_result.bars_used,
                bars=bar_data,
            )
        else:
            fast, slow = int(params["fast_period"]), int(params["slow_period"])
            current_fast_result = calculate_sma(bars, fast)
            previous_fast_result = calculate_sma(bars[:-1], fast)
            current_slow_result = calculate_sma(bars, slow)
            previous_slow_result = calculate_sma(bars[:-1], slow)
            current_fast = current_fast_result.value
            previous_fast = previous_fast_result.value
            current_slow = current_slow_result.value
            previous_slow = previous_slow_result.value
            if None in (current_fast, previous_fast, current_slow, previous_slow):
                return False, {
                    **market_inputs,
                    "indicator_bars_used": max(
                        current_fast_result.bars_used,
                        previous_fast_result.bars_used,
                        current_slow_result.bars_used,
                        previous_slow_result.bars_used,
                    ),
                    "bars": bar_data,
                }, "insufficient_history"
            direction = params["direction"]
            result = (
                previous_fast <= previous_slow and current_fast > current_slow
                if direction == "up"
                else previous_fast >= previous_slow and current_fast < current_slow
            )
            market_inputs.update(
                fast_period=fast, slow_period=slow, direction=direction,
                previous_fast_ma=str(previous_fast), previous_slow_ma=str(previous_slow),
                current_fast_ma=str(current_fast), current_slow_ma=str(current_slow),
                indicator_bars_used=max(
                    current_fast_result.bars_used,
                    previous_fast_result.bars_used,
                    current_slow_result.bars_used,
                    previous_slow_result.bars_used,
                ),
                bars=bar_data,
            )
    elif rule.rule_type in {"thesis_target_hit", "thesis_stop_hit"}:
        if thesis is None:
            return False, market_inputs, "active_thesis_unavailable"
        market_inputs["thesis_status"] = thesis.status
        if rule.rule_type == "thesis_stop_hit":
            stop = thesis.stop_level
            if stop is None:
                return False, {**market_inputs, "thesis_id": str(thesis.id)}, "stop_level_unset"
            result = price <= stop
            market_inputs.update(thesis_id=str(thesis.id), stop_level=str(stop))
        else:
            index = int(params["target_index"])
            if index >= len(thesis.target_levels):
                return False, {**market_inputs, "thesis_id": str(thesis.id)}, "target_level_unavailable"
            target = _decimal(thesis.target_levels[index]["price"])
            result = price >= target
            market_inputs.update(
                thesis_id=str(thesis.id), target_index=index, target_price=str(target),
                target_trim_pct=thesis.target_levels[index]["trim_pct"],
            )
    elif rule.rule_type == "unrealized_gain_pct_above":
        position = ledger.positions.get(symbol) if ledger and symbol else None
        if position is None or position.quantity <= 0 or position.cost_basis <= 0:
            return False, market_inputs, "position_unavailable"
        market_value = price * position.quantity
        gain_pct = (market_value - position.cost_basis) * Decimal("100") / position.cost_basis
        threshold = _decimal(params["threshold_pct"])
        result = gain_pct >= threshold
        market_inputs.update(
            quantity=str(position.quantity), cost_basis=str(position.cost_basis),
            market_value=str(market_value), unrealized_gain_pct=str(gain_pct),
            threshold_pct=str(threshold),
        )
    elif rule.rule_type == "position_weight_above":
        position = ledger.positions.get(symbol) if ledger and symbol else None
        if position is None or position.quantity <= 0:
            return False, market_inputs, "position_unavailable"
        if not params.get("_portfolio_prices_complete", False):
            return False, market_inputs, "portfolio_value_unavailable"
        prices = params.get("_portfolio_prices", {})
        cash = _decimal(params.get("_cash_balance", "0"))
        total_value = cash + sum(
            (_decimal(px) * _decimal(qty) for px, qty in prices.values()), Decimal("0")
        )
        market_value = price * position.quantity
        if total_value <= 0:
            return False, market_inputs, "portfolio_value_unavailable"
        weight = market_value * Decimal("100") / total_value
        threshold = _decimal(params["threshold_pct"])
        result = weight >= threshold
        market_inputs.update(
            quantity=str(position.quantity), market_value=str(market_value),
            portfolio_value=str(total_value), weight_pct=str(weight),
            threshold_pct=str(threshold), cash_balance=str(cash),
            portfolio_price_inputs=params.get("_portfolio_price_inputs", []),
        )
    return result, market_inputs, None


def evaluate_rules(
    db: Session, user_id: UUID, *, now: datetime | None = None
) -> dict[str, Any]:
    evaluated_at = _utc(now or datetime.now(timezone.utc))
    rules = list(
        db.scalars(
            select(AlertRule).where(AlertRule.user_id == user_id, AlertRule.enabled.is_(True))
        )
    )
    quotes_by_instrument: dict[UUID, MarketQuote | None] = {}
    portfolios: dict[UUID, tuple[Portfolio, PortfolioLedger, dict[str, UUID]]] = {}
    result: dict[str, Any] = {"fired": [], "suppressed": [], "deduplicated": [], "cooling_down": []}

    def portfolio_context(portfolio_id: UUID):
        if portfolio_id in portfolios:
            return portfolios[portfolio_id]
        portfolio = db.scalar(
            select(Portfolio).where(Portfolio.id == portfolio_id, Portfolio.user_id == user_id)
        )
        if portfolio is None:
            return None
        ledger = calculate_portfolio_ledger(db, user_id, portfolio.id)
        linked_rows = db.execute(
            select(PortfolioTransaction.symbol, PortfolioTransaction.instrument_id)
            .where(
                PortfolioTransaction.portfolio_id == portfolio.id,
                PortfolioTransaction.instrument_id.is_not(None),
                PortfolioTransaction.transaction_type.in_(( "buy", "sell")),
            )
            .order_by(PortfolioTransaction.occurred_at.desc())
        ).all()
        mapping: dict[str, UUID] = {}
        for symbol, instrument_id in linked_rows:
            if symbol and symbol not in mapping:
                mapping[symbol] = instrument_id
        portfolios[portfolio_id] = (portfolio, ledger, mapping)
        return portfolios[portfolio_id]

    for rule in rules:
        portfolio_ctx = portfolio_context(rule.portfolio_id) if rule.portfolio_id else None
        if rule.portfolio_id and portfolio_ctx is None:
            continue
        portfolio, ledger, linked = portfolio_ctx if portfolio_ctx else (None, None, {})
        if rule.instrument_id is not None:
            instruments = list(
                db.scalars(select(Instrument).where(Instrument.id == rule.instrument_id))
            )
        elif portfolio is not None:
            instrument_ids = {linked[s] for s, p in ledger.positions.items() if p.quantity > 0 and s in linked}
            instruments = list(
                db.scalars(select(Instrument).where(Instrument.id.in_(instrument_ids)))
            ) if instrument_ids else []
        else:
            instruments = []

        holdings_by_instrument: dict[UUID, str] = {}
        if ledger and linked:
            for symbol, position in ledger.positions.items():
                if position.quantity > 0 and symbol in linked:
                    holdings_by_instrument.setdefault(linked[symbol], symbol)
        for instrument in instruments:
            symbol = holdings_by_instrument.get(instrument.id, instrument.symbol)
            if rule.scope == "position" and instrument.id not in holdings_by_instrument:
                continue
            quote = quotes_by_instrument.get(instrument.id)
            if instrument.id not in quotes_by_instrument:
                quote = db.scalar(
                    select(MarketQuote)
                    .where(MarketQuote.instrument_id == instrument.id)
                    .order_by(MarketQuote.retrieved_at.desc())
                    .limit(1)
                )
                quotes_by_instrument[instrument.id] = quote
            thesis = None
            if rule.rule_type in {"thesis_target_hit", "thesis_stop_hit"} and portfolio:
                thesis = db.scalar(
                    select(PositionThesis).where(
                        PositionThesis.user_id == user_id,
                        PositionThesis.portfolio_id == portfolio.id,
                        PositionThesis.instrument_id == instrument.id,
                        PositionThesis.status == "active",
                    )
                )
            params = dict(rule.params)
            if portfolio:
                params["_portfolio_currency"] = portfolio.base_currency
                params["_cash_balance"] = str(ledger.cash_balance)
                price_inputs = {}
                price_input_evidence = []
                prices_complete = True
                for linked_id, linked_symbol in holdings_by_instrument.items():
                    linked_quote = quotes_by_instrument.get(linked_id)
                    if linked_id not in quotes_by_instrument:
                        linked_quote = db.scalar(
                            select(MarketQuote)
                            .where(MarketQuote.instrument_id == linked_id)
                            .order_by(MarketQuote.retrieved_at.desc())
                            .limit(1)
                        )
                        quotes_by_instrument[linked_id] = linked_quote
                    linked_position = ledger.positions[linked_symbol]
                    linked_instrument = db.get(Instrument, linked_id)
                    usable, quote_reason, freshness_evidence = classify_quote(
                        linked_quote,
                        linked_instrument,
                        portfolio.base_currency,
                        evaluated_at,
                    )
                    reliable = usable and linked_instrument is not None
                    if reliable and linked_quote is not None:
                        price_inputs[linked_symbol] = (
                            str(linked_quote.close), str(linked_position.quantity)
                        )
                    else:
                        prices_complete = False
                    price_input_evidence.append({
                        "symbol": linked_symbol,
                        "instrument_id": str(linked_id),
                        "currency": linked_instrument.currency if linked_instrument else None,
                        "quantity": str(linked_position.quantity),
                        "price": str(linked_quote.close) if linked_quote else None,
                        **freshness_evidence,
                        "unusable_reason": quote_reason,
                        "is_synthetic": linked_quote.is_synthetic if linked_quote else None,
                        "included": reliable,
                    })
                params["_portfolio_prices"] = price_inputs
                params["_portfolio_price_inputs"] = price_input_evidence
                params["_portfolio_prices_complete"] = prices_complete
            bar_limit = settings.indicator_lookback_bars if rule.rule_type in DAILY_BAR_RULES else 2
            bars = _bars(db, instrument.id, bar_limit)
            matched, evidence, suppressed = _rule_match(
                rule, instrument, quote, bars, ledger, symbol, thesis, evaluated_at, params
            )
            if suppressed:
                evidence["suppression_reason"] = suppressed
                result["suppressed"].append({
                    "rule_id": str(rule.id), "instrument_id": str(instrument.id),
                    "reason": suppressed, "evidence": evidence,
                })
                continue
            if not matched:
                continue
            if rule.rule_type == "holding_period_crosses_365d":
                prior_event = db.scalar(
                    select(AlertEvent.id).where(
                        AlertEvent.rule_id == rule.id,
                        AlertEvent.instrument_id == instrument.id,
                    ).limit(1)
                )
                if prior_event is not None:
                    result["deduplicated"].append(str(rule.id))
                    continue
            params_hash = hashlib.sha256(
                json.dumps(rule.params, sort_keys=True, default=str).encode()
            ).hexdigest()[:16]
            dedupe_key = f"{instrument.id}:{rule.rule_type}:{params_hash}:{evaluated_at.date().isoformat()}"
            recent = db.scalar(
                select(AlertEvent).where(
                    AlertEvent.rule_id == rule.id,
                    AlertEvent.instrument_id == instrument.id,
                    AlertEvent.fired_at >= evaluated_at - timedelta(seconds=rule.cooldown_seconds),
                ).order_by(AlertEvent.fired_at.desc()).limit(1)
            ) if rule.cooldown_seconds > 0 else None
            if recent is not None:
                result["cooling_down"].append(str(rule.id))
                continue
            duplicate = db.scalar(
                select(AlertEvent.id).where(
                    AlertEvent.rule_id == rule.id, AlertEvent.dedupe_key == dedupe_key
                )
            )
            if duplicate is not None:
                result["deduplicated"].append(str(rule.id))
                continue
            event = AlertEvent(
                rule_id=rule.id,
                user_id=user_id,
                instrument_id=instrument.id,
                fired_at=evaluated_at,
                dedupe_key=dedupe_key,
                evidence={
                    **evidence,
                    "rule_id": str(rule.id),
                    "rule_type": rule.rule_type,
                    "rule_scope": rule.scope,
                    "severity": rule.severity,
                    "evaluated_at": evaluated_at.isoformat(),
                },
                status="new",
            )
            try:
                with db.begin_nested():
                    db.add(event)
                    db.flush()
            except IntegrityError:
                result["deduplicated"].append(str(rule.id))
                continue
            result["fired"].append(event)
    db.commit()
    return result
