"""Live observability writes -- to Postgres, not DuckDB.

See agent/db/observability_models.py for why, and
agent/observability_sync.py for how these rows make it into DuckDB for
query-pattern-mining/cost analysis. Function names/signatures match the
prior DuckDB-backed version exactly (agent/loop.py just needed `await`
added at each call site) -- only the storage target changed.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from agent.db.observability_models import AgentLlmCall, AgentQuery, AgentStep
from agent.db.postgres import async_session_maker


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def start_query(user_id: uuid.UUID, query_text: str) -> uuid.UUID:
    query_id = uuid.uuid4()
    async with async_session_maker() as session:
        session.add(
            AgentQuery(
                query_id=query_id,
                user_id=user_id,
                query_text=query_text,
                status="running",
                started_at=_now(),
            )
        )
        await session.commit()
    return query_id


async def finish_query(query_id: uuid.UUID, status: str, final_answer: str | None) -> None:
    async with async_session_maker() as session:
        query = await session.get(AgentQuery, query_id)
        query.status = status
        query.final_answer = final_answer
        query.finished_at = _now()
        await session.commit()


async def log_step(
    query_id: uuid.UUID,
    phase: str,
    step_index: int,
    *,
    tool_name: str | None = None,
    tool_args: dict | None = None,
    tool_result: str | None = None,
    tool_success: bool | None = None,
    tool_error: str | None = None,
    summary: str | None = None,
) -> uuid.UUID:
    step_id = uuid.uuid4()
    now = _now()
    async with async_session_maker() as session:
        session.add(
            AgentStep(
                step_id=step_id,
                query_id=query_id,
                phase=phase,
                step_index=step_index,
                tool_name=tool_name,
                tool_args=tool_args,
                tool_result=tool_result,
                tool_success=tool_success,
                tool_error=tool_error,
                summary=summary,
                started_at=now,
                finished_at=now,
            )
        )
        await session.commit()
    return step_id


async def log_llm_call(step_id: uuid.UUID, model: str, usage: dict) -> None:
    async with async_session_maker() as session:
        session.add(
            AgentLlmCall(
                call_id=uuid.uuid4(),
                step_id=step_id,
                model=model,
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
                cache_read_tokens=usage.get("cache_read_tokens"),
                cache_write_tokens=usage.get("cache_write_tokens"),
                called_at=_now(),
            )
        )
        await session.commit()
