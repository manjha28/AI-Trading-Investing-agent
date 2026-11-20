from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.accounting import CorporateActionAdjustment
from app.models import (
    CorporateAction,
    CorporateActionApplication,
    CorporateActionCoverage,
    Instrument,
    MarketBar,
)
from app.provider_adapters import YahooFinanceProvider


class CorporateActionConflictError(ValueError):
    pass


class CorporateActionCoverageError(ValueError):
    pass


@dataclass(frozen=True)
class AdjustedBar:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source: str
    is_synthetic: bool
    adjustment_factor: Decimal
    applied_action_ids: tuple[UUID, ...]
    dividends_included: bool


def _yfinance_ticker(instrument: Instrument) -> str:
    for identifier in getattr(instrument, "provider_identifiers", ()):
        if identifier.provider == "yfinance" and identifier.provider_symbol:
            return identifier.provider_symbol
    suffix = {"NSE": ".NS", "BSE": ".BO"}.get(instrument.venue.upper())
    if suffix is not None:
        return instrument.symbol.removesuffix("-EQ") + suffix
    if instrument.venue.upper() in {"NMS", "NCM", "NYQ", "PCX", "XNGS", "XNYS"}:
        return instrument.symbol
    raise ValueError(f"yfinance corporate actions are unsupported for venue {instrument.venue}")


class PrefetchedActionsProvider:
    """Serves ``get_corporate_actions(ticker)`` from a bulk download, so a whole-universe sweep needs a
    handful of requests instead of one per symbol. A ticker the download failed for raises, so it is
    reported as unverified rather than silently treated as "no actions"."""

    def __init__(self, actions: dict[str, list[dict[str, Any]]], failed: set[str] | None = None) -> None:
        self._actions = actions
        self._failed = failed or set()

    def get_corporate_actions(self, ticker: str) -> list[dict[str, Any]]:
        if ticker in self._failed or ticker not in self._actions:
            raise LookupError(f"no bulk result for {ticker}")
        return self._actions[ticker]


def _yahoo_bulk_download(tickers: list[str], period: str):
    import yfinance as yf

    return yf.download(
        tickers, period=period, interval="1d", actions=True, group_by="ticker",
        auto_adjust=False, progress=False, threads=True,
    )


def fetch_recent_actions(
    tickers: list[str], *, period: str = "1mo", chunk_size: int = 40, pause_seconds: float = 1.0,
    downloader: Any = None,
) -> tuple[dict[str, list[dict[str, Any]]], set[str]]:
    """Dividends and splits from the last ``period`` for many tickers, in a few bulk requests.

    Returns (events by ticker, tickers that could not be fetched). Events have the same shape as
    ``YahooFinanceProvider.get_corporate_actions``.
    """
    import time

    downloader = downloader or _yahoo_bulk_download
    events: dict[str, list[dict[str, Any]]] = {}
    failed: set[str] = set()
    for start in range(0, len(tickers), chunk_size):
        chunk = tickers[start:start + chunk_size]
        try:
            frame = downloader(chunk, period)
        except Exception:
            failed.update(chunk)
            continue
        multi = hasattr(frame, "columns") and getattr(frame.columns, "nlevels", 1) > 1
        for ticker in chunk:
            try:
                sub = frame[ticker] if multi else frame
                if sub.empty or sub["Close"].dropna().empty:
                    failed.add(ticker)
                    continue
                found: list[dict[str, Any]] = []
                for stamp, row in sub.iterrows():
                    ex_date = stamp.date() if hasattr(stamp, "date") else date.fromisoformat(str(stamp)[:10])
                    for column, action_type in (("Stock Splits", "split"), ("Dividends", "dividend")):
                        raw = row.get(column)
                        if raw is None or str(raw).casefold() in {"nan", "none", "<na>"}:
                            continue
                        value = Decimal(str(raw))
                        if not value.is_finite() or value <= 0:
                            continue
                        found.append({
                            "ex_date": ex_date, "action_type": action_type,
                            "ratio": value if action_type == "split" else None,
                            "amount": value if action_type == "dividend" else None,
                        })
                events[ticker] = found
            except Exception:
                failed.add(ticker)
        if start + chunk_size < len(tickers) and pause_seconds:
            time.sleep(pause_seconds)
    return events, failed


def sync_yfinance_corporate_actions(
    db: Session, user_id: UUID, *, provider: YahooFinanceProvider | None = None,
    instrument_ids: set[UUID] | None = None, record_coverage: bool = True,
) -> dict[str, Any]:
    """Fetch action events for every instrument with stored daily or 1m bars.

    ``instrument_ids`` restricts the sweep (the scheduler passes only what the user holds or
    watches); ``None`` keeps the original behaviour of checking every instrument with bars.
    ``record_coverage=False`` is for recent-window sweeps: events are stored, but the instrument's
    full-history coverage is NOT marked verified (a one-month look-back says nothing about history).
    """
    provider = provider or YahooFinanceProvider()
    statement = (
        select(Instrument)
        .join(MarketBar, MarketBar.instrument_id == Instrument.id)
        .where(
            MarketBar.interval.in_(("1d", "1m")),
            MarketBar.is_synthetic.is_(False),
        )
        .options(selectinload(Instrument.provider_identifiers))
        .distinct()
        .order_by(Instrument.venue, Instrument.symbol)
    )
    if instrument_ids is not None:
        if not instrument_ids:
            return {"instruments_checked": 0, "actions_inserted": 0, "instruments_unverified": 0, "instruments": []}
        statement = statement.where(Instrument.id.in_(list(instrument_ids)))
    instruments = db.scalars(statement).all()
    report: list[dict[str, Any]] = []
    total_inserted = 0
    for instrument in instruments:
        checked_at = datetime.now(timezone.utc)
        coverage = db.scalar(select(CorporateActionCoverage).where(
            CorporateActionCoverage.user_id == user_id,
            CorporateActionCoverage.instrument_id == instrument.id,
            CorporateActionCoverage.source == "yfinance",
        )) if record_coverage else None
        if coverage is None:
            coverage = CorporateActionCoverage(
                user_id=user_id,
                instrument_id=instrument.id,
                source="yfinance",
                status="unverified",
                checked_at=checked_at,
                source_reference=instrument.symbol,
            )
            if record_coverage:
                db.add(coverage)  # a transient object otherwise: attribute updates below are discarded
        try:
            ticker = _yfinance_ticker(instrument)
            events = provider.get_corporate_actions(ticker)
        except Exception as error:
            coverage.status = "unverified"
            coverage.checked_at = checked_at
            coverage.source_reference = coverage.source_reference or instrument.symbol
            coverage.detail = f"{type(error).__name__}: {error}"[:500]
            report.append({
                "instrument_id": str(instrument.id), "symbol": instrument.symbol,
                "venue": instrument.venue, "source": "yfinance", "status": "unverified",
                "actions_returned": None, "actions_inserted": 0,
                "source_reference": coverage.source_reference, "detail": coverage.detail,
            })
            continue
        try:
            inserted = 0
            for event in events:
                ex_date = event["ex_date"]
                action_type = event["action_type"]
                ratio = event.get("ratio")
                amount = event.get("amount")
                reference = f"{ticker}:{ex_date.isoformat()}:{action_type}:{ratio or ''}:{amount or ''}"
                existing_ingested = db.scalar(select(CorporateAction).where(
                    CorporateAction.user_id == user_id,
                    CorporateAction.instrument_id == instrument.id,
                    CorporateAction.source == "yfinance",
                    CorporateAction.source_reference == reference,
                ))
                if existing_ingested is not None:
                    if existing_ingested.verified_at is None:
                        existing_ingested.verified_at = checked_at
                    continue
                existing = db.scalars(select(CorporateAction).where(
                    CorporateAction.user_id == user_id,
                    CorporateAction.instrument_id == instrument.id,
                    CorporateAction.ex_date == ex_date,
                    CorporateAction.action_type == action_type,
                )).all()
                incoming = (ratio, amount)
                conflicts = [row for row in existing if (row.ratio, row.amount) != incoming]
                if conflicts:
                    note = "Conflicting corporate action source values; retain both for owner review."
                    for row in conflicts:
                        row.is_discrepant = True
                        row.discrepancy_note = note
                action = CorporateAction(
                    user_id=user_id,
                    instrument_id=instrument.id,
                    action_type=action_type,
                    ex_date=ex_date,
                    ratio=ratio,
                    amount=amount,
                    source="yfinance",
                    source_reference=reference,
                    verified_at=checked_at,
                    verified_by=None,
                    is_discrepant=bool(conflicts),
                    discrepancy_note=(
                        "Conflicting corporate action source values; retain both for owner review."
                        if conflicts else None
                    ),
                    created_at=checked_at,
                )
                db.add(action)
                inserted += 1
            total_inserted += inserted
            coverage.status = "verified"
            coverage.checked_at = checked_at
            coverage.source_reference = ticker
            coverage.detail = f"Retrieved {len(events)} action rows; inserted {inserted} new rows."
            report.append({
                "instrument_id": str(instrument.id), "symbol": instrument.symbol,
                "venue": instrument.venue, "source": "yfinance", "status": "verified",
                "actions_returned": len(events), "actions_inserted": inserted,
                "source_reference": ticker,
            })
        except Exception:
            db.rollback()
            raise
    db.commit()
    return {
        "instruments_checked": len(instruments),
        "actions_inserted": total_inserted,
        "instruments_unverified": sum(row["status"] == "unverified" for row in report),
        "instruments": report,
    }


def adjusted_bars_for_backtest(
    db: Session,
    user_id: UUID,
    instrument: Instrument,
    bars: list[MarketBar],
    *,
    include_dividends: bool = False,
    raw_price_basis_verified: bool = False,
) -> list[AdjustedBar]:
    """Return a non-destructive backward-adjusted series with per-bar action lineage."""
    if not bars:
        return []
    source_names = {bar.source for bar in bars}
    if len(source_names) != 1:
        raise CorporateActionCoverageError("A derived series must use one unambiguous bar source.")
    coverage = db.scalar(select(CorporateActionCoverage).where(
        CorporateActionCoverage.user_id == user_id,
        CorporateActionCoverage.instrument_id == instrument.id,
        CorporateActionCoverage.source == "yfinance",
    ))
    if coverage is None or coverage.status != "verified":
        raise CorporateActionCoverageError(
            f"Corporate actions for {instrument.symbol} are unverified; backtest is unavailable."
        )
    if not raw_price_basis_verified:
        raise CorporateActionCoverageError(
            f"Raw split/bonus basis for {bars[0].source} bars is not verified. "
            "No adjustment was applied because provider OHLC may already include corporate-action "
            "adjustments."
        )
    zone = ZoneInfo(instrument.timezone)
    def session_date(value: datetime) -> date:
        aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return aware.astimezone(zone).date()

    first_date, last_date = session_date(bars[0].timestamp), session_date(bars[-1].timestamp)
    actions = db.scalars(select(CorporateAction).where(
        CorporateAction.user_id == user_id,
        CorporateAction.instrument_id == instrument.id,
        CorporateAction.ex_date > first_date,
        CorporateAction.ex_date <= last_date,
    ).order_by(CorporateAction.ex_date, CorporateAction.id)).all()
    by_date_type: dict[tuple[date, str], list[CorporateAction]] = {}
    for action in actions:
        by_date_type.setdefault((action.ex_date, action.action_type), []).append(action)
    events: dict[date, list[tuple[Decimal, Decimal, tuple[CorporateAction, ...]]]] = {}
    mechanical_by_date: dict[date, Decimal] = {}
    for (ex_date, action_type), records in by_date_type.items():
        if action_type in {"split", "bonus"}:
            record = records[0]
            if record.ratio is not None:
                mechanical_by_date[ex_date] = mechanical_by_date.get(ex_date, Decimal("1")) * (
                    record.ratio if action_type == "split" else Decimal("1") + record.ratio
                )
    for (ex_date, action_type), records in by_date_type.items():
        if any(row.is_discrepant for row in records):
            raise CorporateActionConflictError(
                f"Conflicting {action_type} records on {ex_date} for {instrument.symbol}."
            )
        terms = {(row.ratio, row.amount) for row in records}
        if len(terms) != 1:
            raise CorporateActionConflictError(
                f"Conflicting {action_type} records on {ex_date} for {instrument.symbol}."
            )
        record = records[0]
        if action_type == "rights":
            raise CorporateActionCoverageError(
                f"Rights action on {ex_date} has no supported adjustment formula; backtest is unavailable."
            )
        if action_type in {"split", "bonus"}:
            if record.ratio is None or record.ratio <= 0:
                raise CorporateActionCoverageError(f"Invalid {action_type} ratio on {ex_date}.")
            share_multiplier = record.ratio if action_type == "split" else Decimal("1") + record.ratio
            price_factor = Decimal("1") / share_multiplier
            events.setdefault(ex_date, []).append((price_factor, share_multiplier, tuple(records)))
        elif action_type == "dividend" and include_dividends:
            if record.amount is None or record.amount <= 0:
                raise CorporateActionCoverageError(f"Invalid dividend amount on {ex_date}.")
            prior = [bar for bar in bars if session_date(bar.timestamp) < ex_date]
            if not prior:
                raise CorporateActionCoverageError(
                    f"No prior close is available to adjust dividend on {ex_date}."
                )
            basis_close = prior[-1].close / mechanical_by_date.get(ex_date, Decimal("1"))
            if basis_close <= record.amount:
                raise CorporateActionCoverageError(
                    f"Dividend adjustment is invalid on {ex_date}; prior close does not exceed dividend."
                )
            factor = (basis_close - record.amount) / basis_close
            events.setdefault(ex_date, []).append((factor, Decimal("1"), tuple(records)))

    output: list[AdjustedBar] = []
    for bar in bars:
        bar_date = session_date(bar.timestamp)
        factor = Decimal("1")
        volume_multiplier = Decimal("1")
        ids: list[UUID] = []
        for ex_date in sorted(events):
            if ex_date <= bar_date:
                continue
            for price_factor, shares, records in events[ex_date]:
                factor *= price_factor
                volume_multiplier *= shares
                ids.extend(row.id for row in records)
        output.append(AdjustedBar(
            timestamp=bar.timestamp,
            open=bar.open * factor,
            high=bar.high * factor,
            low=bar.low * factor,
            close=bar.close * factor,
            volume=bar.volume * volume_multiplier,
            source=bar.source,
            is_synthetic=bar.is_synthetic,
            adjustment_factor=factor,
            applied_action_ids=tuple(ids),
            dividends_included=include_dividends,
        ))
    return output


def get_portfolio_corporate_actions(
    db: Session,
    portfolio_id: UUID,
    user_id: UUID,
    *,
    through_date: date | None = None,
) -> list[CorporateActionAdjustment]:
    statement = (
        select(CorporateAction, Instrument.symbol)
        .join(
            CorporateActionApplication,
            CorporateActionApplication.corporate_action_id == CorporateAction.id,
        )
        .join(Instrument, Instrument.id == CorporateAction.instrument_id)
        .where(
            CorporateActionApplication.portfolio_id == portfolio_id,
            CorporateActionApplication.user_id == user_id,
            CorporateAction.user_id == user_id,
            CorporateActionApplication.reversed_at.is_(None),
        )
        .order_by(CorporateAction.ex_date, CorporateAction.id)
    )
    if through_date is not None:
        statement = statement.where(CorporateAction.ex_date <= through_date)
    return [
        CorporateActionAdjustment(
            id=action.id,
            symbol=symbol,
            action_type=action.action_type,
            ex_date=action.ex_date,
            ratio=action.ratio,
            amount=action.amount,
        )
        for action, symbol in db.execute(statement).all()
    ]
