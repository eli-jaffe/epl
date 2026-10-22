"""Live observability writes -- to Postgres, not DuckDB.

See agent/db/observability_models.py for why, and
agent/observability_sync.py for how these rows make it into DuckDB for
query-pattern-mining/cost analysis. Function names/signatures match the
prior DuckDB-backed version exactly (agent/loop.py just needed `await`
added at each call site) -- only the storage target changed.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, update

from agent.db.observability_models import AgentLlmCall, AgentQuery, AgentStep
from agent.db.postgres import async_session_maker


# A running query whose last heartbeat is older than this is presumed dead
# (backend killed, task hung). Steps are logged after every phase/tool call,
# so a healthy query's gap is at most one LLM call or run_sql (10s cap) --
# 10 minutes leaves generous slack. The same threshold is used at startup
# rather than "mark everything running as dead": the MCP server runs the
# agent loop in a separate process, and a restart of the web backend must
# not interrupt a live MCP-driven query.
STALE_THRESHOLD = timedelta(minutes=10)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def start_query(
    user_id: uuid.UUID, query_text: str, conversation_id: uuid.UUID | None = None
) -> uuid.UUID:
    query_id = uuid.uuid4()
    async with async_session_maker() as session:
        session.add(
            AgentQuery(
                query_id=query_id,
                user_id=user_id,
                conversation_id=conversation_id,
                query_text=query_text,
                status="running",
                started_at=_now(),
                last_heartbeat_at=_now(),
            )
        )
        await session.commit()
    return query_id


async def finish_query(query_id: uuid.UUID, status: str, final_answer: str | None) -> None:
    async with async_session_maker() as session:
        query = await session.get(AgentQuery, query_id)
        # Only a still-running query may be finished: if the sweep already
        # flipped it to 'interrupted' (or a user cancelled it), a straggling
        # loop must not overwrite that decision.
        if query.status != "running":
            return
        query.status = status
        query.final_answer = final_answer
        query.finished_at = _now()
        await session.commit()


async def log_step(
    query_id: uuid.UUID,
    phase: str,
    step_index: int,
    started_at: datetime,
    *,
    tool_name: str | None = None,
    tool_args: dict | None = None,
    tool_result: str | None = None,
    tool_success: bool | None = None,
    tool_error: str | None = None,
    summary: str | None = None,
) -> uuid.UUID:
    step_id = uuid.uuid4()
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
                started_at=started_at,
                finished_at=_now(),
            )
        )
        await session.execute(update(AgentQuery).where(AgentQuery.query_id == query_id).values(last_heartbeat_at=_now()))
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


async def mark_stale_queries(threshold: timedelta = STALE_THRESHOLD) -> int:
    """Flips `running` queries with no heartbeat within `threshold` to
    'interrupted' and returns how many. Rows without a heartbeat (written
    before the column existed) are judged by started_at instead.

    Resets synced_at so agent/observability_sync.py (which only copies rows
    where it's NULL) pushes the new status to DuckDB.
    """
    cutoff = _now() - threshold
    async with async_session_maker() as session:
        result = await session.execute(
            update(AgentQuery)
            .where(
                AgentQuery.status == "running",
                func.coalesce(AgentQuery.last_heartbeat_at, AgentQuery.started_at) < cutoff,
            )
            .values(status="interrupted", synced_at=None)
        )
        await session.commit()
        return result.rowcount
