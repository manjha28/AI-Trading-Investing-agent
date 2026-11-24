from datetime import date, datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import threading
import time

from app import research_service
from app.api.auth import get_active_session, require_csrf
from app.config import settings
from app.db import get_db
from app.models import (
    AiStockAnalysis,
    Instrument,
    InstrumentResearchBrief,
    InstrumentResearchRun,
    InstrumentSourceLink,
    ResearchNote,
    ResearchSource,
    UserSession,
)
from app.services import ai_analyst, fundamentals_feed, research_pipeline, screener_import
from app.services.research_scoring import HORIZONS

router = APIRouter(prefix="/research", tags=["research"])

DISCLAIMER = "Decision support, not investment advice. Verify every fact at its source."
SOURCE_FETCHES_PER_MINUTE = 10


def require_verified_session(
    active_session: UserSession = Depends(get_active_session),
) -> UserSession:
    if not active_session.user.email_verified:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "email_verification_required",
                "message": "Verify your email address before using research features.",
            },
        )
    return active_session


class SourceCreate(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    instrument_id: UUID | None = None
    publisher: str | None = Field(default=None, max_length=200)
    title: str | None = Field(default=None, max_length=500)
    author: str | None = Field(default=None, max_length=200)
    published_at: datetime | None = None


class SourceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    instrument_id: UUID | None
    url: str
    publisher: str | None
    title: str | None
    author: str | None
    published_at: datetime | None
    fetched_at: datetime
    content_hash: str
    excerpt: str | None
    created_at: datetime
    deduplicated: bool = False


class FactInput(BaseModel):
    statement: str = Field(min_length=1, max_length=1000)
    as_of: date
    source_ids: list[UUID] = Field(default_factory=list)

    @field_validator("as_of")
    @classmethod
    def not_future(cls, value: date) -> date:
        if value > datetime.now(timezone.utc).date():
            raise ValueError("A fact cannot be dated in the future")
        return value


class NoteCreate(BaseModel):
    instrument_id: UUID
    as_of: date
    summary: str = Field(min_length=1, max_length=4000)
    key_facts: list[FactInput] = Field(default_factory=list)
    confidence: Literal["low", "medium", "high"]

    @field_validator("as_of")
    @classmethod
    def not_future(cls, value: date) -> date:
        if value > datetime.now(timezone.utc).date():
            raise ValueError("A note cannot be dated in the future")
        return value


class NoteUpdate(BaseModel):
    as_of: date | None = None
    summary: str | None = Field(default=None, min_length=1, max_length=4000)
    key_facts: list[FactInput] | None = None
    confidence: Literal["low", "medium", "high"] | None = None

    @field_validator("as_of")
    @classmethod
    def not_future(cls, value: date | None) -> date | None:
        if value is not None and value > datetime.now(timezone.utc).date():
            raise ValueError("A note cannot be dated in the future")
        return value


class FactResponse(BaseModel):
    statement: str
    as_of: date
    source_ids: list[UUID]
    stale: bool


class NoteResponse(BaseModel):
    id: UUID
    instrument_id: UUID
    as_of: date
    summary: str
    key_facts: list[FactResponse]
    source_ids: list[UUID]
    confidence: str
    stale: bool
    created_at: datetime
    updated_at: datetime
    disclaimer: str = DISCLAIMER


class InstrumentResearchResponse(BaseModel):
    instrument_id: UUID
    notes: list[NoteResponse]
    sources: list[SourceResponse]
    disclaimer: str = DISCLAIMER


class ResearchBriefResponse(BaseModel):
    run_id: UUID
    instrument_id: UUID
    status: Literal["running", "complete", "partial", "failed"]
    started_at: datetime
    finished_at: datetime | None
    sources_attempted: int
    sources_succeeded: int
    failures: list[dict[str, str]]
    renderer: Literal["template", "ai"]
    sections: dict
    data_quality: list[dict[str, str]]
    created_at: datetime
    disclaimer: str = DISCLAIMER


class ResearchRunStatusResponse(BaseModel):
    run_id: UUID
    instrument_id: UUID
    status: Literal["running", "complete", "partial", "failed", "cancelled"]
    started_at: datetime
    finished_at: datetime | None
    sources_attempted: int
    sources_succeeded: int
    failures: list[dict[str, str]]
    progress: list[dict]
    cancel_requested: bool
    brief: ResearchBriefResponse | None = None
    reused: bool = False


class SourceLinkCreate(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    label: str = Field(min_length=1, max_length=200)


class SourceLinkUpdate(BaseModel):
    enabled: bool


class SourceLinkResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    instrument_id: UUID
    url: str
    label: str
    enabled: bool
    created_at: datetime


def _note_response(note: ResearchNote) -> NoteResponse:
    today = datetime.now(timezone.utc).date()
    facts = [
        FactResponse(
            statement=fact["statement"],
            as_of=date.fromisoformat(fact["as_of"]),
            source_ids=[UUID(item) for item in fact["source_ids"]],
            stale=research_service.is_stale(date.fromisoformat(fact["as_of"]), today),
        )
        for fact in note.key_facts
    ]
    return NoteResponse(
        id=note.id,
        instrument_id=note.instrument_id,
        as_of=note.as_of,
        summary=note.summary,
        key_facts=facts,
        source_ids=[UUID(item) for item in note.source_ids],
        confidence=note.confidence,
        stale=research_service.is_stale(note.as_of, today) or any(f.stale for f in facts),
        created_at=note.created_at,
        updated_at=note.updated_at,
    )


def _require_instrument(db: Session, instrument_id: UUID) -> None:
    if db.get(Instrument, instrument_id) is None:
        raise HTTPException(status_code=404, detail="Instrument not found")


def _owned_note(db: Session, user_id: UUID, note_id: UUID) -> ResearchNote:
    note = db.scalar(
        select(ResearchNote).where(ResearchNote.id == note_id, ResearchNote.user_id == user_id)
    )
    if note is None:
        raise HTTPException(status_code=404, detail="Research note not found")
    return note


def _facts_payload(
    db: Session, user_id: UUID, facts: list[FactInput]
) -> tuple[list[dict], list[str]]:
    payload = [fact.model_dump(mode="json") for fact in facts]
    try:
        cited = research_service.resolve_fact_sources(db, user_id, payload)
    except research_service.ResearchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return payload, [str(item) for item in cited]


@router.get("/sources", response_model=list[SourceResponse])
def list_sources(
    instrument_id: UUID | None = None,
    limit: int = Query(default=100, ge=1, le=200),
    active_session: UserSession = Depends(require_verified_session),
    db: Session = Depends(get_db),
) -> list[ResearchSource]:
    query = select(ResearchSource).where(ResearchSource.user_id == active_session.user_id)
    if instrument_id is not None:
        query = query.where(ResearchSource.instrument_id == instrument_id)
    return list(db.scalars(query.order_by(ResearchSource.fetched_at.desc()).limit(limit)))


@router.post("/sources", response_model=SourceResponse, status_code=201)
def create_source(
    payload: SourceCreate,
    response: Response,
    active_session: UserSession = Depends(require_verified_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> SourceResponse:
    user_id = active_session.user_id
    if payload.instrument_id is not None:
        _require_instrument(db, payload.instrument_id)
    now = datetime.now(timezone.utc)
    recent = db.scalar(
        select(func.count())
        .select_from(ResearchSource)
        .where(
            ResearchSource.user_id == user_id,
            ResearchSource.created_at > now - timedelta(minutes=1),
        )
    )
    if recent and recent >= SOURCE_FETCHES_PER_MINUTE:
        raise HTTPException(status_code=429, detail="Too many sources added. Try again shortly.")
    try:
        document = research_service.fetch_document(payload.url)
    except research_service.ResearchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    existing = db.scalar(
        select(ResearchSource).where(
            ResearchSource.user_id == user_id,
            ResearchSource.content_hash == document.content_hash,
        )
    )
    if existing is None:
        source = ResearchSource(
            user_id=user_id,
            instrument_id=payload.instrument_id,
            url=document.url,
            publisher=payload.publisher,
            title=payload.title or document.title,
            author=payload.author,
            published_at=payload.published_at,
            fetched_at=document.fetched_at,
            content_hash=document.content_hash,
            excerpt=document.excerpt,
            created_at=now,
        )
        db.add(source)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = db.scalar(
                select(ResearchSource).where(
                    ResearchSource.user_id == user_id,
                    ResearchSource.content_hash == document.content_hash,
                )
            )
        else:
            db.refresh(source)
            return SourceResponse.model_validate(source)
    response.status_code = 200
    result = SourceResponse.model_validate(existing)
    result.deduplicated = True
    return result


@router.get("/notes", response_model=list[NoteResponse])
def list_notes(
    instrument_id: UUID | None = None,
    active_session: UserSession = Depends(require_verified_session),
    db: Session = Depends(get_db),
) -> list[NoteResponse]:
    query = select(ResearchNote).where(ResearchNote.user_id == active_session.user_id)
    if instrument_id is not None:
        query = query.where(ResearchNote.instrument_id == instrument_id)
    return [_note_response(n) for n in db.scalars(query.order_by(ResearchNote.as_of.desc()))]


@router.post("/notes", response_model=NoteResponse, status_code=201)
def create_note(
    payload: NoteCreate,
    active_session: UserSession = Depends(require_verified_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> NoteResponse:
    _require_instrument(db, payload.instrument_id)
    facts, source_ids = _facts_payload(db, active_session.user_id, payload.key_facts)
    now = datetime.now(timezone.utc)
    note = ResearchNote(
        user_id=active_session.user_id,
        instrument_id=payload.instrument_id,
        as_of=payload.as_of,
        summary=payload.summary,
        key_facts=facts,
        source_ids=source_ids,
        confidence=payload.confidence,
        created_at=now,
        updated_at=now,
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return _note_response(note)


@router.get("/notes/{note_id}", response_model=NoteResponse)
def get_note(
    note_id: UUID,
    active_session: UserSession = Depends(require_verified_session),
    db: Session = Depends(get_db),
) -> NoteResponse:
    return _note_response(_owned_note(db, active_session.user_id, note_id))


@router.patch("/notes/{note_id}", response_model=NoteResponse)
def update_note(
    note_id: UUID,
    payload: NoteUpdate,
    active_session: UserSession = Depends(require_verified_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> NoteResponse:
    note = _owned_note(db, active_session.user_id, note_id)
    changes = payload.model_dump(exclude_unset=True)
    if "key_facts" in changes:
        if payload.key_facts is None:
            raise HTTPException(status_code=422, detail="key_facts cannot be null")
        note.key_facts, note.source_ids = _facts_payload(
            db, active_session.user_id, payload.key_facts
        )
    for field in ("as_of", "summary", "confidence"):
        if field in changes:
            if changes[field] is None:
                raise HTTPException(status_code=422, detail=f"{field} cannot be null")
            setattr(note, field, changes[field])
    note.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(note)
    return _note_response(note)


@router.delete("/notes/{note_id}", status_code=204)
def delete_note(
    note_id: UUID,
    active_session: UserSession = Depends(require_verified_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    db.delete(_owned_note(db, active_session.user_id, note_id))
    db.commit()
    return Response(status_code=204)


@router.get("/instruments/{instrument_id}", response_model=InstrumentResearchResponse)
def instrument_research(
    instrument_id: UUID,
    active_session: UserSession = Depends(require_verified_session),
    db: Session = Depends(get_db),
) -> InstrumentResearchResponse:
    _require_instrument(db, instrument_id)
    user_id = active_session.user_id
    notes = list(
        db.scalars(
            select(ResearchNote)
            .where(ResearchNote.user_id == user_id, ResearchNote.instrument_id == instrument_id)
            .order_by(ResearchNote.as_of.desc())
        )
    )
    cited = {UUID(item) for note in notes for item in note.source_ids}
    query = select(ResearchSource).where(ResearchSource.user_id == user_id)
    if cited:
        query = query.where(
            (ResearchSource.instrument_id == instrument_id) | ResearchSource.id.in_(cited)
        )
    else:
        query = query.where(ResearchSource.instrument_id == instrument_id)
    sources = list(db.scalars(query.order_by(ResearchSource.fetched_at.desc())))
    return InstrumentResearchResponse(
        instrument_id=instrument_id,
        notes=[_note_response(n) for n in notes],
        sources=[SourceResponse.model_validate(s) for s in sources],
    )


def _brief_response(
    run: InstrumentResearchRun, brief: InstrumentResearchBrief
) -> ResearchBriefResponse:
    return ResearchBriefResponse(
        run_id=run.id,
        instrument_id=run.instrument_id,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        sources_attempted=run.sources_attempted,
        sources_succeeded=run.sources_succeeded,
        failures=run.failures,
        renderer=brief.renderer,
        sections=brief.sections,
        data_quality=brief.data_quality,
        created_at=brief.created_at,
    )


def _run_status_response(
    db: Session, run: InstrumentResearchRun, *, reused: bool = False
) -> ResearchRunStatusResponse:
    brief = db.scalar(select(InstrumentResearchBrief).where(
        InstrumentResearchBrief.run_id == run.id
    ))
    return ResearchRunStatusResponse(
        run_id=run.id, instrument_id=run.instrument_id, status=run.status,
        started_at=run.started_at, finished_at=run.finished_at,
        sources_attempted=run.sources_attempted, sources_succeeded=run.sources_succeeded,
        failures=run.failures, progress=run.progress or [], cancel_requested=run.cancel_requested,
        brief=_brief_response(run, brief) if brief is not None else None, reused=reused,
    )


@router.post(
    "/instruments/{instrument_id}/run",
    response_model=ResearchRunStatusResponse,
    status_code=202,
)
def run_instrument_research(
    instrument_id: UUID,
    background_tasks: BackgroundTasks,
    active_session: UserSession = Depends(require_verified_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ResearchRunStatusResponse:
    instrument = db.get(Instrument, instrument_id)
    if instrument is None:
        raise HTTPException(status_code=404, detail="Instrument not found")
    if instrument.asset_class != "equity" or instrument.country.upper() != "IN" or instrument.venue.upper() not in {
        "NSE", "XNSE", "NSI", "NSE_EQ", "BSE", "XBOM", "BOM", "BSE_EQ",
    }:
        raise HTTPException(status_code=422, detail="Research orchestration supports NSE/BSE equities only.")
    run, created = research_pipeline.start_instrument_research(
        db, active_session.user_id, instrument
    )
    if created:
        background_tasks.add_task(research_pipeline.process_instrument_research, run.id)
    return _run_status_response(db, run, reused=not created)


@router.get("/runs/{run_id}", response_model=ResearchRunStatusResponse)
def get_research_run_status(
    run_id: UUID,
    active_session: UserSession = Depends(require_verified_session),
    db: Session = Depends(get_db),
) -> ResearchRunStatusResponse:
    run = db.scalar(select(InstrumentResearchRun).where(
        InstrumentResearchRun.id == run_id,
        InstrumentResearchRun.user_id == active_session.user_id,
    ))
    if run is None:
        raise HTTPException(status_code=404, detail="Research run not found")
    research_pipeline.recover_stale_research_run(run, db)
    return _run_status_response(db, run)


@router.post("/runs/{run_id}/cancel", response_model=ResearchRunStatusResponse)
def cancel_research_run(
    run_id: UUID,
    active_session: UserSession = Depends(require_verified_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ResearchRunStatusResponse:
    run = db.scalar(select(InstrumentResearchRun).where(
        InstrumentResearchRun.id == run_id,
        InstrumentResearchRun.user_id == active_session.user_id,
    ))
    if run is None:
        raise HTTPException(status_code=404, detail="Research run not found")
    if run.status == "running":
        run.cancel_requested = True
        run.progress = [*(run.progress or []), {
            "component": "orchestrator", "state": "cancellation_requested",
            "detail": "The current source request will finish before the worker stops at a component boundary.",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }]
        db.commit()
        db.refresh(run)
    return _run_status_response(db, run)


@router.get(
    "/instruments/{instrument_id}/brief",
    response_model=ResearchBriefResponse,
)
def get_instrument_research_brief(
    instrument_id: UUID,
    active_session: UserSession = Depends(require_verified_session),
    db: Session = Depends(get_db),
) -> ResearchBriefResponse:
    if db.get(Instrument, instrument_id) is None:
        raise HTTPException(status_code=404, detail="Instrument not found")
    result = research_pipeline.latest_brief(
        db, active_session.user_id, instrument_id
    )
    if result is None:
        raise HTTPException(status_code=404, detail="No research brief is available")
    return _brief_response(*result)


@router.get(
    "/instruments/{instrument_id}/source-links",
    response_model=list[SourceLinkResponse],
)
def list_instrument_source_links(
    instrument_id: UUID,
    active_session: UserSession = Depends(require_verified_session),
    db: Session = Depends(get_db),
) -> list[InstrumentSourceLink]:
    if db.get(Instrument, instrument_id) is None:
        raise HTTPException(status_code=404, detail="Instrument not found")
    return list(
        db.scalars(
            select(InstrumentSourceLink)
            .where(
                InstrumentSourceLink.user_id == active_session.user_id,
                InstrumentSourceLink.instrument_id == instrument_id,
            )
            .order_by(InstrumentSourceLink.created_at, InstrumentSourceLink.id)
        )
    )


@router.post(
    "/instruments/{instrument_id}/source-links",
    response_model=SourceLinkResponse,
    status_code=201,
)
def create_instrument_source_link(
    instrument_id: UUID,
    payload: SourceLinkCreate,
    response: Response,
    active_session: UserSession = Depends(require_verified_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> InstrumentSourceLink:
    if db.get(Instrument, instrument_id) is None:
        raise HTTPException(status_code=404, detail="Instrument not found")
    try:
        _host, _port, normalized_url = research_service.validate_source_url(payload.url)
    except research_service.ResearchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    existing = db.scalar(
        select(InstrumentSourceLink).where(
            InstrumentSourceLink.user_id == active_session.user_id,
            InstrumentSourceLink.instrument_id == instrument_id,
            InstrumentSourceLink.url == normalized_url,
        )
    )
    if existing is not None:
        response.status_code = 200
        return existing
    link = InstrumentSourceLink(
        user_id=active_session.user_id,
        instrument_id=instrument_id,
        url=normalized_url,
        label=payload.label.strip(),
        enabled=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(link)
    db.commit()
    db.refresh(link)
    return link


@router.patch(
    "/source-links/{link_id}",
    response_model=SourceLinkResponse,
)
def update_instrument_source_link(
    link_id: UUID,
    payload: SourceLinkUpdate,
    active_session: UserSession = Depends(require_verified_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> InstrumentSourceLink:
    link = db.scalar(
        select(InstrumentSourceLink).where(
            InstrumentSourceLink.id == link_id,
            InstrumentSourceLink.user_id == active_session.user_id,
        )
    )
    if link is None:
        raise HTTPException(status_code=404, detail="Source link not found")
    link.enabled = payload.enabled
    db.commit()
    db.refresh(link)
    return link


@router.delete("/source-links/{link_id}", status_code=204)
def delete_instrument_source_link(
    link_id: UUID,
    active_session: UserSession = Depends(require_verified_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    link = db.scalar(
        select(InstrumentSourceLink).where(
            InstrumentSourceLink.id == link_id,
            InstrumentSourceLink.user_id == active_session.user_id,
        )
    )
    if link is None:
        raise HTTPException(status_code=404, detail="Source link not found")
    db.delete(link)
    db.commit()
    return Response(status_code=204)


# ───────────────────────── Optional AI analyst (user-triggered) ─────────────────────────
AI_DISCLAIMER = (
    "AI analysis is a model-generated judgement over the stored snapshot, shown separately from the "
    "algorithmic score. Decision support, not investment advice."
)
_ai_in_flight: set[tuple[UUID, UUID, str]] = set()
_ai_in_flight_lock = threading.Lock()


class AiAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    horizon: Literal["swing", "positional", "long_term"]


def _ai_row_response(row: AiStockAnalysis, *, cached: bool, comparison: dict | None = None) -> dict:
    return {
        "id": str(row.id), "brief_id": str(row.brief_id), "instrument_id": str(row.instrument_id),
        "horizon": row.horizon, "provider": row.model.partition(":")[0],
        "model": row.model.partition(":")[2] or row.model, "prompt_version": row.prompt_version,
        "snapshot_hash": row.snapshot_hash, "source": row.source, "created_at": row.created_at,
        "cached": cached, "result": row.result, "usage": row.usage,
        "comparison": comparison if comparison is not None else (row.result or {}).get("comparison"),
        "disclaimer": AI_DISCLAIMER,
    }


def _ai_status() -> dict:
    key = settings.gemini_api_key
    model = ai_analyst.configured_model()
    return {
        "enabled": settings.ai_enabled,
        "configured": bool(key and key.get_secret_value() and model),
        "provider": ai_analyst.PROVIDER,
        "model": model or settings.gemini_model,
    }


@router.get("/instruments/{instrument_id}/ai-analysis")
def get_ai_analyses(
    instrument_id: UUID,
    active_session: UserSession = Depends(require_verified_session),
    db: Session = Depends(get_db),
) -> dict:
    """Stored analyses for the latest research snapshot. Never calls the AI provider."""
    if db.get(Instrument, instrument_id) is None:
        raise HTTPException(status_code=404, detail="Instrument not found")
    latest = research_pipeline.latest_brief(db, active_session.user_id, instrument_id)
    rows = []
    if latest is not None:
        rows = list(db.scalars(select(AiStockAnalysis).where(
            AiStockAnalysis.user_id == active_session.user_id,
            AiStockAnalysis.brief_id == latest[1].id,
            AiStockAnalysis.model == ai_analyst.model_id(),
            AiStockAnalysis.prompt_version == ai_analyst.PROMPT_VERSION,
        )))
    return {
        **_ai_status(), "brief_id": str(latest[1].id) if latest else None,
        # Stored analyses for horizons Research no longer supports (e.g. intraday) stay in the table, untouched, but are not served.
        "analyses": {row.horizon: _ai_row_response(row, cached=True) for row in rows if row.horizon in HORIZONS},
        "disclaimer": AI_DISCLAIMER,
    }


@router.post("/instruments/{instrument_id}/ai-analysis")
def run_ai_analysis(
    instrument_id: UUID,
    payload: AiAnalysisRequest,
    active_session: UserSession = Depends(require_verified_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict:
    status_info = _ai_status()
    if not status_info["enabled"]:
        raise HTTPException(status_code=503, detail={"code": "ai_disabled", "message": "AI analysis is disabled on this server."})
    if not status_info["configured"]:
        raise HTTPException(status_code=503, detail={"code": "ai_not_configured", "message": "No Gemini API key (GEMINI_API_KEY) or valid GEMINI_MODEL is configured on the server."})
    instrument = db.get(Instrument, instrument_id)
    if instrument is None:
        raise HTTPException(status_code=404, detail="Instrument not found")
    latest = research_pipeline.latest_brief(db, active_session.user_id, instrument_id)
    if latest is None:
        raise HTTPException(status_code=409, detail={"code": "no_research_snapshot", "message": "Run research first; AI analysis reads a stored snapshot."})
    _run, brief = latest
    cache_filter = (
        AiStockAnalysis.user_id == active_session.user_id, AiStockAnalysis.brief_id == brief.id,
        AiStockAnalysis.horizon == payload.horizon, AiStockAnalysis.model == ai_analyst.model_id(),
        AiStockAnalysis.prompt_version == ai_analyst.PROMPT_VERSION,
    )
    existing = db.scalar(select(AiStockAnalysis).where(*cache_filter))
    if existing is not None:
        return _ai_row_response(existing, cached=True)

    key = (active_session.user_id, brief.id, payload.horizon)
    with _ai_in_flight_lock:
        if key in _ai_in_flight:
            raise HTTPException(status_code=409, detail={"code": "ai_in_progress", "message": "This analysis is already running."})
        _ai_in_flight.add(key)
    try:
        now = ai_analyst.utcnow()
        pack = ai_analyst.build_evidence_pack(
            brief.sections, instrument=instrument, horizon=payload.horizon, brief_created_at=brief.created_at, now=now,
        )
        usage = None
        if ai_analyst.has_usable_evidence(pack):
            try:
                result, usage = ai_analyst.analyse(pack, payload.horizon, settings.gemini_api_key.get_secret_value())  # type: ignore[union-attr]
            except ai_analyst.AiAnalysisError as error:
                raise HTTPException(status_code=error.status_code, detail={"code": error.code, "message": error.message}) from error
            source = "gemini"
        else:
            result, source = ai_analyst.local_insufficient_result(pack), "local_gate"
        comparison = ai_analyst.compare_with_algorithm(result, pack)
        row = AiStockAnalysis(
            user_id=active_session.user_id, brief_id=brief.id, instrument_id=instrument.id, horizon=payload.horizon,
            model=ai_analyst.model_id(), prompt_version=ai_analyst.PROMPT_VERSION,
            snapshot_hash=ai_analyst.snapshot_hash(pack), source=source,
            result={**result, "comparison": comparison},
            usage=usage, created_at=now,
        )
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            row = db.scalar(select(AiStockAnalysis).where(*cache_filter))
            if row is None:
                raise
            return _ai_row_response(row, cached=True)
        db.refresh(row)
        return _ai_row_response(row, cached=False, comparison=comparison)
    finally:
        with _ai_in_flight_lock:
            _ai_in_flight.discard(key)


# ───────────────────────── Optional Screener gap-fill (owner-supplied page) ─────────────────────────
class ScreenerImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    html: str = Field(min_length=200, max_length=screener_import.MAX_HTML_BYTES)


@router.get("/instruments/{instrument_id}/fundamentals-gaps")
def get_fundamentals_gaps(
    instrument_id: UUID,
    _session: UserSession = Depends(require_verified_session),
    db: Session = Depends(get_db),
) -> dict:
    if db.get(Instrument, instrument_id) is None:
        raise HTTPException(status_code=404, detail="Instrument not found")
    instrument = db.get(Instrument, instrument_id)
    return {**screener_import.fundamentals_gaps(db, instrument_id), "coverage": fundamentals_feed.instrument_coverage(db, instrument),
            "disclaimer": DISCLAIMER}


LEVEL_KEEP = ("status", "reason", "entry_zone", "stop", "targets", "entry_status", "entry_label", "reference_close", "prior_high_reference", "ready_to_buy", "event_gate", "selected_setup", "direction", "watch_band", "pivot_support", "pivot_resistance", "gates", "breakout_trigger")
HEAVY_CONFIDENCE_KEYS = ("parts", "setup", "note")


def _compact_row(row: dict) -> dict:
    """The list view of a research card: the ladder, state, score and one-line confidence; the long derivations and the "why" parts come from GET /daily-report/{instrument_id}."""
    def slim_conf(c):
        return {k: v for k, v in c.items() if k not in HEAVY_CONFIDENCE_KEYS} if isinstance(c, dict) else c
    out = dict(row)
    # the list keeps what a row and the ladder need; the derivations, support/resistance lists and pivots come with the full card
    out["levels"] = {h: ({k: v for k, v in lv.items() if k in LEVEL_KEEP} if isinstance(lv, dict) else lv) for h, lv in (row.get("levels") or {}).items()}
    out["horizons"] = {h: ({**hv, "buy_confidence": slim_conf(hv.get("buy_confidence"))} if isinstance(hv, dict) else hv) for h, hv in (row.get("horizons") or {}).items()}
    out["confidence"] = slim_conf(row.get("confidence"))
    out["compact"] = True
    return out


def _compact_report(report: dict) -> dict:
    return {**report, "buckets": {b: [_compact_row(r) for r in rows] for b, rows in (report.get("buckets") or {}).items()}, "compact": True}


@router.get("/daily-report/{instrument_id}")
def get_daily_report_card(
    instrument_id: str,
    _session: UserSession = Depends(require_verified_session),
    db: Session = Depends(get_db),
):
    """One research card in full (levels derivation, the "why" parts and setup checks) for the detail panel."""
    from app.services import report_store
    served = report_store.serve(db, _session.user_id)
    for rows in (served["payload"].get("buckets") or {}).values():
        for row in rows:
            if row.get("instrument_id") == instrument_id:
                return JSONResponse(row, headers={"Cache-Control": "private, max-age=30"})
    raise HTTPException(status_code=404, detail="That stock is not in the daily report.")


@router.get("/daily-report")
def get_daily_report(
    request: Request,
    compact: bool = Query(default=True, description="List view: omits the long derivations and the 'why' parts (fetch one card in full from /daily-report/{instrument_id}). compact=false returns everything."),
    bucket: str | None = Query(default=None, pattern="^(buy|setup_waiting|radar|hold|no_new_entry|stale_data|not_enough_data)$"),
    limit: int | None = Query(default=None, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    _session: UserSession = Depends(require_verified_session),
    db: Session = Depends(get_db),
):
    """Buy / hold / no-new-entry buckets over the owner's watchlist and held stocks, from the latest stored research. The report is built once after each research run and stored; this returns the stored
    copy (ETag, gzip), rebuilding in the background when it is stale. ``bucket`` (+ ``limit``/``offset``) returns one page of one bucket."""
    from app import research_daily
    from app.services import report_store
    researching = research_daily.kick_missing(_session.user_id)   # newly added stocks start researching immediately
    served = report_store.serve(db, _session.user_id)
    etag = f'"{served["etag"]}-{bucket or "all"}-{limit or 0}-{offset}-{"c" if compact else "f"}"'
    headers = {"ETag": etag, "Cache-Control": "private, max-age=0, must-revalidate"}
    if request.headers.get("if-none-match") == etag and not researching:
        return Response(status_code=304, headers=headers)
    report = _compact_report(served["payload"]) if compact else served["payload"]
    if bucket:
        rows = (report.get("buckets") or {}).get(bucket, [])
        page = rows[offset: offset + limit] if limit else rows[offset:]
        body = {**{k: v for k, v in report.items() if k != "buckets"}, "buckets": {bucket: page}, "page": {"bucket": bucket, "offset": offset, "returned": len(page), "total": len(rows)}}
    else:
        body = report
    return JSONResponse({**body, "researching": researching, "built_at": served["built_at"].isoformat(), "rebuilding": served["rebuilding"]}, headers=headers)


@router.get("/pipeline-v2/compare", include_in_schema=False)
def pipeline_v2_compare(
    limit: int = Query(default=300, ge=1, le=2000),
    _session: UserSession = Depends(require_verified_session),
    db: Session = Depends(get_db),
) -> dict:
    """Hidden (not in the API docs or the UI): the owner's latest v1-vs-v2 recommendation per stock and horizon, from the shadow records. Owner-scoped, read-only."""
    from app.services import pipeline_v2
    return {**pipeline_v2.compare(db, _session.user_id, limit=limit), "disclaimer": DISCLAIMER}


@router.get("/multibagger")
def get_multibagger(
    klass: str | None = Query(default=None, alias="class", pattern="^(multibagger_candidate|quality_compounder|emerging|new_listing|watch|not_a_fit|not_tradable|avoid|not_assessable)$"),
    sector: str | None = Query(default=None, max_length=80),
    min_rank: float | None = Query(default=None, ge=0, le=100),
    limit: int = Query(default=400, ge=1, le=1000),
    _session: UserSession = Depends(require_verified_session),
    db: Session = Depends(get_db),
) -> dict:
    """Phase 6 multibagger screen: the latest nightly result per stock of the owner's research universe. Always readable; ``shadow`` is true while ``multibagger_screen_enabled`` is off (the results are then computed nightly but not used by any card or verdict).
    Long-term only: swing and positional are not filtered by fundamentals. Owner-scoped, read-only."""
    from app.models.multibagger import MultibaggerAssessment
    shadow = not settings.multibagger_screen_enabled      # the flag decides whether the engine is USED (long-term Quality stage, cards); the owner can always read the nightly results here
    latest = db.scalar(select(func.max(MultibaggerAssessment.as_of)).where(MultibaggerAssessment.user_id == _session.user_id))
    if latest is None:
        return {"enabled": True, "shadow": shadow, "as_of": None, "counts": {}, "items": [], "message": "No nightly result has been computed yet.", "disclaimer": DISCLAIMER}
    rows = db.execute(select(MultibaggerAssessment, Instrument.symbol).join(Instrument, Instrument.id == MultibaggerAssessment.instrument_id)
                      .where(MultibaggerAssessment.user_id == _session.user_id, MultibaggerAssessment.as_of == latest)).all()
    counts: dict[str, int] = {}
    items = []
    for a, sym in rows:
        counts[a.klass] = counts.get(a.klass, 0) + 1
        c = a.checks or {}
        if klass and a.klass != klass or (min_rank is not None and (a.rank or 0) < min_rank):
            continue
        if sector and sector.casefold() not in str(c.get("kind", "")).casefold() and sector.casefold() not in str(c.get("sector", "")).casefold():
            continue
        all_checks = c.get("checks") or []
        unknown_ids = [k["id"] for k in all_checks if k.get("result") == "unknown"]
        items.append({"symbol": sym, "instrument_id": str(a.instrument_id), "class": a.klass, "rank": a.rank, "scores": {"Q": a.q, "G": a.g, "V": a.v, "T": a.t, "X": a.x}, "checklist": c.get("checklist"),
                      "checks_passed": c.get("checks_passed"), "checks_total": c.get("checks_total"), "unknown_count": len(unknown_ids), "unknown_ids": unknown_ids,
                      "fail_count": sum(1 for k in all_checks if k.get("result") == "fail"),
                      "timing": c.get("timing"), "kind": c.get("kind"), "reasons": (c.get("reasons") or [])[:3],
                      "failed_or_unknown": [f"{k['id']}: {k['reason']}" for k in (c.get("checks") or []) if k["result"] in ("fail", "unknown")][:8], "class_if_unknown_checks_cleared": (c.get("class_if_unknown_checks_cleared") or {}).get("class")})
    order = {"multibagger_candidate": 0, "quality_compounder": 1, "emerging": 2, "new_listing": 3, "watch": 4, "not_a_fit": 5, "not_tradable": 6, "avoid": 7, "not_assessable": 8}
    items.sort(key=lambda i: (order[i["class"]], -(i["rank"] or 0), i["symbol"]))
    return {"enabled": True, "shadow": shadow, "as_of": latest.isoformat(), "model_version": rows[0][0].model_version if rows else None, "counts": counts, "items": items[:limit],
            "universe_rule": f"{len(rows)} stocks assessed (funds and ETFs excluded): the same stock universe as the Daily report; the Market watch lists more instruments because it includes funds and ETFs.", "disclaimer": DISCLAIMER}


@router.get("/multibagger/{symbol}")
def get_multibagger_detail(
    symbol: str,
    _session: UserSession = Depends(require_verified_session),
    db: Session = Depends(get_db),
) -> dict:
    """One stock's latest multibagger assessment with EVERY check (passed, failed, unknown) and its one-line reason, for the screen's detail panel. Owner-scoped, read-only."""
    from app.models.multibagger import MultibaggerAssessment
    row = db.execute(select(MultibaggerAssessment, Instrument.symbol).join(Instrument, Instrument.id == MultibaggerAssessment.instrument_id)
                     .where(MultibaggerAssessment.user_id == _session.user_id, func.upper(Instrument.symbol) == symbol.upper())
                     .order_by(MultibaggerAssessment.as_of.desc()).limit(1)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="No multibagger assessment for this stock.")
    a, sym = row
    c = a.checks or {}
    return {"symbol": sym, "class": a.klass, "as_of": a.as_of.isoformat(), "model_version": a.model_version, "scores": {"Q": a.q, "G": a.g, "V": a.v, "T": a.t, "X": a.x}, "rank": a.rank,
            "kind": c.get("kind"), "timing": c.get("timing"), "reasons": c.get("reasons") or [], "checklist": c.get("checklist"), "checks": c.get("checks") or [], "emerging": c.get("emerging"),
            "class_if_unknown_checks_cleared": c.get("class_if_unknown_checks_cleared"), "disclaimer": DISCLAIMER}


@router.get("/stock/{symbol}/verdict")
def get_stock_verdict(
    symbol: str,
    _session: UserSession = Depends(require_verified_session),
    db: Session = Depends(get_db),
) -> dict:
    """Phase 7: the latest nightly stock verdict (seven lenses, a verdict, an evidence score and levels per horizon, a headline = the best horizon). Always readable (``shadow`` is true while ``stock_verdict_enabled`` is off); the nightly
    shadow run stores results regardless. Owner-scoped, read-only. Decision support, not investment advice."""
    from app.models.stock_verdict import StockVerdict
    shadow = not settings.stock_verdict_enabled          # off = computed nightly and readable here, but the Daily report buckets do not use it
    inst = db.scalar(select(Instrument).where(func.upper(Instrument.symbol) == symbol.upper(), Instrument.is_synthetic.is_(False)).order_by(Instrument.venue.desc()).limit(1))
    if inst is None:
        raise HTTPException(status_code=404, detail="Instrument not found")
    latest = db.scalar(select(func.max(StockVerdict.as_of)).where(StockVerdict.user_id == _session.user_id, StockVerdict.instrument_id == inst.id))
    if latest is None:
        return {"enabled": True, "shadow": shadow, "symbol": inst.symbol, "as_of": None, "message": "No nightly verdict has been computed for this stock yet.", "horizons": {}, "disclaimer": DISCLAIMER}
    rows = list(db.scalars(select(StockVerdict).where(StockVerdict.user_id == _session.user_id, StockVerdict.instrument_id == inst.id, StockVerdict.as_of == latest)))
    horizons = {}
    for r in rows:
        extra = r.reasons or {}
        horizons[r.horizon] = {"verdict": r.verdict, "evidence_score": r.evidence_score, "calibrated_confidence": r.calibrated_confidence, "confidence": extra.get("confidence"), "evidence": extra.get("evidence"),
                               "reasons": (extra.get("reasons") or [])[:3], "veto": extra.get("veto"), "size": extra.get("size"), "levels": extra.get("levels"), "grade": extra.get("grade"), "lenses": r.lens}
    any_row = (rows[0].reasons or {}) if rows else {}
    return {"enabled": True, "shadow": shadow, "symbol": inst.symbol, "as_of": latest.isoformat(), "model_version": rows[0].model_version if rows else None, "headline": any_row.get("headline"), "horizons": horizons,
            "multibagger": any_row.get("multibagger"), "regime": any_row.get("regime"), "risks_and_events": any_row.get("risks_and_events"), "disclaimer": DISCLAIMER}


@router.get("/forward-record", include_in_schema=False)
def get_forward_record(
    _session: UserSession = Depends(require_verified_session),
    db: Session = Depends(get_db),
) -> dict:
    """Hidden (not in the API docs or the navigation): the latest weekly forward-record report from the owner's point-in-time shadow verdicts and classes. Owner-scoped, read-only."""
    from app.services import forward_record
    rep = forward_record.latest(db, _session.user_id)
    if rep is None:
        return {"available": False, "message": "No weekly forward-record report has been stored yet; the job runs weekly after the nightly shadow run.", "disclaimer": DISCLAIMER}
    return {"available": True, **rep, "disclaimer": DISCLAIMER}


@router.get("/fundamentals-coverage")
def get_fundamentals_coverage(
    limit: int = Query(default=200, ge=1, le=1000),
    _session: UserSession = Depends(require_verified_session),
    db: Session = Depends(get_db),
) -> dict:
    """Overall and per-stock fundamentals coverage, last retrieval per source, and unresolved gaps."""
    return {**fundamentals_feed.coverage_report(db, limit=limit), "disclaimer": DISCLAIMER}


@router.post("/instruments/{instrument_id}/screener-import")
def import_screener_page(
    instrument_id: UUID,
    payload: ScreenerImportRequest,
    _session: UserSession = Depends(require_verified_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> dict:
    """Fill gaps from a Screener company page the owner saved from their own browser. Never fetches screener.in."""
    instrument = db.get(Instrument, instrument_id)
    if instrument is None:
        raise HTTPException(status_code=404, detail="Instrument not found")
    try:
        parsed = screener_import.parse_screener_html(payload.html)
        before = screener_import.fundamentals_gaps(db, instrument_id)
        result = screener_import.apply_gap_fill(db, instrument, parsed)
    except screener_import.ScreenerParseError as error:
        db.rollback()
        raise HTTPException(status_code=422, detail={"code": str(error), "message": "The pasted page could not be used safely."}) from error
    return {
        "basis": parsed.basis, "source": screener_import.SOURCE, **result.as_dict(),
        "gaps_before": before, "gaps_after": screener_import.fundamentals_gaps(db, instrument_id),
        "disclaimer": DISCLAIMER,
    }
