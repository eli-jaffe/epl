"""Chat endpoint -- the Claude-driven Reason->Plan->Execute->Reflect->
Synthesize agent loop (agent.loop.run_agent_query), see
docs/agent_ui_architecture_plan.md.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from agent.auth.models import User
from agent.auth.users import current_active_user
from agent.loop import run_agent_query

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatRequest(BaseModel):
    query: str


class ChatResponse(BaseModel):
    answer: str


@router.post("", response_model=ChatResponse)
async def chat(request: ChatRequest, user: User = Depends(current_active_user)) -> ChatResponse:
    answer = await run_agent_query(user.id, request.query)
    return ChatResponse(answer=answer)
