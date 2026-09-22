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

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agent.auth.models import User
from agent.auth.users import current_active_user
from agent.loop import run_agent_query

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatRequest(BaseModel):
    query: str


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
