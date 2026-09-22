"""Admin observability API -- read-only views over the live Postgres
agent_query/agent_step/agent_llm_call tables (agent/db/observability_models.py),
not the DuckDB sync copy (agent/observability_sync.py) -- that copy can lag
by up to the sync interval and is meant for query-pattern-mining/cost
analysis, not "what did the agent just do." Every route requires
`current_superuser` (agent/auth/users.py) -- promote an account with
`python -m agent.auth.promote_admin <email>`.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.auth.models import User
from agent.auth.users import current_superuser
from agent.db.observability_models import AgentLlmCall, AgentQuery, AgentStep
from agent.db.postgres import get_async_session

router = APIRouter(prefix="/admin", tags=["admin"])


def _duration_ms(started_at: datetime | None, finished_at: datetime | None) -> int | None:
    if started_at is None or finished_at is None:
        return None
    return int((finished_at - started_at).total_seconds() * 1000)


class QuerySummary(BaseModel):
    query_id: uuid.UUID
    user_email: str
    query_text: str
    status: str
    started_at: datetime
    duration_ms: int | None


class LlmCallOut(BaseModel):
    call_id: uuid.UUID
    model: str
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None


class StepOut(BaseModel):
    step_id: uuid.UUID
    phase: str
    step_index: int
    tool_name: str | None
    tool_args: dict | None
    tool_result: str | None
    tool_success: bool | None
    tool_error: str | None
    summary: str | None
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    llm_calls: list[LlmCallOut]


class QueryDetail(BaseModel):
    query_id: uuid.UUID
    user_email: str
    query_text: str
    status: str
    final_answer: str | None
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    steps: list[StepOut]


@router.get("/queries", response_model=list[QuerySummary])
async def list_queries(
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    user_email: str | None = None,
    session: AsyncSession = Depends(get_async_session),
    _admin: User = Depends(current_superuser),
) -> list[QuerySummary]:
    stmt = select(AgentQuery, User.email).join(User, AgentQuery.user_id == User.id)
    if status is not None:
        stmt = stmt.where(AgentQuery.status == status)
    if user_email is not None:
        stmt = stmt.where(User.email.ilike(f"%{user_email}%"))
    stmt = stmt.order_by(AgentQuery.started_at.desc()).limit(limit).offset(offset)

    rows = (await session.execute(stmt)).all()
    return [
        QuerySummary(
            query_id=q.query_id,
            user_email=email,
            query_text=q.query_text,
            status=q.status,
            started_at=q.started_at,
            duration_ms=_duration_ms(q.started_at, q.finished_at),
        )
        for q, email in rows
    ]


@router.get("/queries/{query_id}", response_model=QueryDetail)
async def get_query_detail(
    query_id: uuid.UUID,
    session: AsyncSession = Depends(get_async_session),
    _admin: User = Depends(current_superuser),
) -> QueryDetail:
    row = (
        await session.execute(
            select(AgentQuery, User.email).join(User, AgentQuery.user_id == User.id).where(AgentQuery.query_id == query_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Query not found")
    query, user_email = row

    steps = (
        (await session.execute(select(AgentStep).where(AgentStep.query_id == query_id).order_by(AgentStep.step_index)))
        .scalars()
        .all()
    )

    llm_calls_by_step: dict[uuid.UUID, list[AgentLlmCall]] = {}
    if steps:
        step_ids = [s.step_id for s in steps]
        llm_calls = (
            (await session.execute(select(AgentLlmCall).where(AgentLlmCall.step_id.in_(step_ids)))).scalars().all()
        )
        for call in llm_calls:
            llm_calls_by_step.setdefault(call.step_id, []).append(call)

    return QueryDetail(
        query_id=query.query_id,
        user_email=user_email,
        query_text=query.query_text,
        status=query.status,
        final_answer=query.final_answer,
        started_at=query.started_at,
        finished_at=query.finished_at,
        duration_ms=_duration_ms(query.started_at, query.finished_at),
        steps=[
            StepOut(
                step_id=s.step_id,
                phase=s.phase,
                step_index=s.step_index,
                tool_name=s.tool_name,
                tool_args=s.tool_args,
                tool_result=s.tool_result,
                tool_success=s.tool_success,
                tool_error=s.tool_error,
                summary=s.summary,
                started_at=s.started_at,
                finished_at=s.finished_at,
                duration_ms=_duration_ms(s.started_at, s.finished_at),
                llm_calls=[
                    LlmCallOut(
                        call_id=c.call_id,
                        model=c.model,
                        input_tokens=c.input_tokens,
                        output_tokens=c.output_tokens,
                        cache_read_tokens=c.cache_read_tokens,
                        cache_write_tokens=c.cache_write_tokens,
                    )
                    for c in llm_calls_by_step.get(s.step_id, [])
                ],
            )
            for s in steps
        ],
    )
