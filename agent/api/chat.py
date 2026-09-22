"""Chat endpoint -- the Claude-driven Reason->Plan->Execute->Reflect->
Synthesize agent loop (agent.loop.run_agent_query), see
docs/agent_ui_architecture_plan.md.

Streams as SSE (text/event-stream) rather than returning one JSON blob:
the loop already knows what phase it's in (agent.loop.STATUS_MESSAGES),
this just surfaces that to the browser in real time instead of making the
user stare at a static "Thinking..." for the whole call. The final answer
(or an error) is still delivered whole, as one terminal frame -- only
status is streamed, not the answer text itself.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent.auth.models import User
from agent.auth.users import current_active_user
from agent.db.observability_models import AgentQuery
from agent.db.postgres import get_async_session
from agent.loop import run_agent_query

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatRequest(BaseModel):
    query: str


class HistoryEntry(BaseModel):
    query_id: uuid.UUID
    query_text: str
    final_answer: str | None
    status: str
    started_at: datetime


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


async def _stream_answer(user_id, query_text: str):
    # One queue carries both status updates and the terminal event (answer
    # or error) -- a plain `await queue.get()` loop, no polling, so status
    # frames reach the client the instant a phase starts rather than on
    # some fixed tick, and nothing queued can be dropped between the
    # background task finishing and the loop noticing.
    queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

    async def on_status(message: str) -> None:
        await queue.put(("status", message))

    async def run() -> None:
        try:
            answer = await run_agent_query(user_id, query_text, on_status=on_status)
            await queue.put(("answer", answer))
        except Exception as exc:  # noqa: BLE001 -- deliberately broad: any
            # failure in the loop becomes a clean terminal SSE frame instead
            # of a raw 500 or a hung connection, per this plan's
            # verification requirement.
            await queue.put(("error", str(exc)))

    task = asyncio.create_task(run())

    while True:
        kind, payload = await queue.get()
        if kind == "status":
            yield _sse({"type": "status", "message": payload})
            continue
        if kind == "answer":
            yield _sse({"type": "answer", "answer": payload})
        else:
            yield _sse({"type": "error", "message": payload})
        break

    await task


@router.post("")
async def chat(request: ChatRequest, user: User = Depends(current_active_user)) -> StreamingResponse:
    return StreamingResponse(
        _stream_answer(user.id, request.query),
        media_type="text/event-stream",
    )


@router.get("/history", response_model=list[HistoryEntry])
async def get_history(
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(current_active_user),
) -> list[HistoryEntry]:
    # Own-history only (no admin gate) -- filtered to this user's rows and
    # excludes `running` so a stale/orphaned row from a crashed session
    # never shows as permanently "in progress" (see the plan's Out of
    # scope note; no resume-in-progress-query flow is built).
    stmt = (
        select(AgentQuery)
        .where(AgentQuery.user_id == user.id, AgentQuery.status != "running")
        .order_by(AgentQuery.started_at.desc())
        .limit(20)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        HistoryEntry(
            query_id=q.query_id,
            query_text=q.query_text,
            final_answer=q.final_answer,
            status=q.status,
            started_at=q.started_at,
        )
        for q in rows
    ]
