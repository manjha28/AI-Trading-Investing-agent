from datetime import date, datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import research_service
from app.api.auth import get_active_session, require_csrf
from app.db import get_db
from app.models import (
    Instrument,
    InstrumentResearchBrief,
    InstrumentResearchRun,
    InstrumentSourceLink,
    ResearchNote,
    ResearchSource,
    UserSession,
)
from app.services import research_pipeline

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


@router.post(
    "/instruments/{instrument_id}/run",
    response_model=ResearchBriefResponse,
)
def run_instrument_research(
    instrument_id: UUID,
    active_session: UserSession = Depends(require_verified_session),
    _: UserSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> ResearchBriefResponse:
    instrument = db.get(Instrument, instrument_id)
    if instrument is None:
        raise HTTPException(status_code=404, detail="Instrument not found")
    run, brief = research_pipeline.run_instrument_research(
        db, active_session.user_id, instrument
    )
    return _brief_response(run, brief)


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
