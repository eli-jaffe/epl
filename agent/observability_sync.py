"""Periodic batch sync: unsynced observability rows in Postgres -> DuckDB.

Run as `python -m agent.observability_sync`, intended to be invoked every
15 minutes by cron/launchd (not installed by this codebase -- an operator
decision, see docs/agent_ui_architecture_plan.md). Mirrors this repo's
existing ingestion pattern (ingestion/load_historical.py): land data, then
idempotent upsert, never delete-then-reinsert.

Why this exists instead of writing straight to DuckDB from the live app:
see agent/db/duckdb.py's docstring and
.claude/plans/vast-hopping-origami.md. Short version: the live app stays
strictly read-only against db/epl.duckdb; this is the only process that
ever opens it for writing, and it does so briefly and infrequently so the
live app's short-lived readers aren't starved.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

import duckdb
from sqlalchemy import select, update

from agent.config import settings
from agent.db.observability_models import AgentLlmCall, AgentQuery, AgentStep
from agent.db.postgres import async_session_maker

MAX_CONNECT_ATTEMPTS = 10
RETRY_DELAY_SECONDS = 3.0


def _connect_with_retry() -> duckdb.DuckDBPyConnection:
    """DuckDB needs sole access to the file to open read-write -- a
    short-lived reader in the live app could transiently hold it. Retry
    rather than fail the whole sync on one bad-timing attempt.
    """
    last_err: Exception | None = None
    for _attempt in range(MAX_CONNECT_ATTEMPTS):
        try:
            return duckdb.connect(str(settings.duckdb_path), read_only=False)
        except duckdb.IOException as e:
            last_err = e
            time.sleep(RETRY_DELAY_SECONDS)
    raise RuntimeError(
        f"Could not acquire the DuckDB write lock after {MAX_CONNECT_ATTEMPTS} "
        f"attempts (~{MAX_CONNECT_ATTEMPTS * RETRY_DELAY_SECONDS:.0f}s) -- giving "
        "up this cycle. Nothing is lost: unsynced rows stay unsynced and the "
        "next scheduled run will pick them up."
    ) from last_err


def _upsert_query(conn: duckdb.DuckDBPyConnection, q: dict) -> None:
    conn.execute(
        "INSERT INTO agent_query (query_id, user_id, query_text, status, started_at, finished_at, final_answer) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (query_id) DO UPDATE SET status = excluded.status, "
        "finished_at = excluded.finished_at, final_answer = excluded.final_answer",
        [q["query_id"], q["user_id"], q["query_text"], q["status"], q["started_at"], q["finished_at"], q["final_answer"]],
    )


def _upsert_step(conn: duckdb.DuckDBPyConnection, s: dict) -> None:
    conn.execute(
        "INSERT INTO agent_step (step_id, query_id, phase, step_index, tool_name, tool_args, "
        "tool_result, tool_success, tool_error, summary, started_at, finished_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (step_id) DO UPDATE SET tool_success = excluded.tool_success, "
        "tool_result = excluded.tool_result, "
        "tool_error = excluded.tool_error, summary = excluded.summary, finished_at = excluded.finished_at",
        [
            s["step_id"], s["query_id"], s["phase"], s["step_index"], s["tool_name"],
            json.dumps(s["tool_args"]) if s["tool_args"] is not None else None,
            s["tool_result"],
            s["tool_success"], s["tool_error"], s["summary"], s["started_at"], s["finished_at"],
        ],
    )


def _estimate_cost(conn: duckdb.DuckDBPyConnection, model: str, l: dict) -> float | None:
    """Cost is computed here, not on the live app's hot path, since the
    pricing table (agent_model_pricing) only exists in DuckDB and there's
    no reason to touch DuckDB from a live request anymore.
    """
    row = conn.execute(
        "SELECT input_usd_per_mtok, output_usd_per_mtok, cache_read_usd_per_mtok, "
        "cache_write_usd_per_mtok FROM agent_model_pricing "
        "WHERE model = ? AND effective_date <= current_date "
        "ORDER BY effective_date DESC LIMIT 1",
        [model],
    ).fetchone()
    if row is None:
        return None
    input_rate, output_rate, cache_read_rate, cache_write_rate = row
    mtok = 1_000_000
    return (
        (l["input_tokens"] or 0) / mtok * input_rate
        + (l["output_tokens"] or 0) / mtok * output_rate
        + (l["cache_read_tokens"] or 0) / mtok * cache_read_rate
        + (l["cache_write_tokens"] or 0) / mtok * cache_write_rate
    )


def _upsert_llm_call(conn: duckdb.DuckDBPyConnection, l: dict) -> None:
    cost_usd = _estimate_cost(conn, l["model"], l)
    conn.execute(
        "INSERT INTO agent_llm_call (call_id, step_id, model, input_tokens, output_tokens, "
        "cache_read_tokens, cache_write_tokens, cost_usd, called_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (call_id) DO UPDATE SET cost_usd = excluded.cost_usd",
        [
            l["call_id"], l["step_id"], l["model"], l["input_tokens"], l["output_tokens"],
            l["cache_read_tokens"], l["cache_write_tokens"], cost_usd, l["called_at"],
        ],
    )


async def sync_once() -> dict[str, int]:
    """Copies every currently-unsynced row to DuckDB, in dependency order
    (query -> step -> llm_call) so a row's parent always exists in DuckDB
    before the row referencing it is inserted. Returns counts synced.
    """
    async with async_session_maker() as session:
        query_rows = (await session.execute(select(AgentQuery).where(AgentQuery.synced_at.is_(None)))).scalars().all()
        query_data = [
            dict(
                query_id=r.query_id, user_id=r.user_id, query_text=r.query_text, status=r.status,
                started_at=r.started_at, finished_at=r.finished_at, final_answer=r.final_answer,
            )
            for r in query_rows
        ]
        step_rows = (await session.execute(select(AgentStep).where(AgentStep.synced_at.is_(None)))).scalars().all()
        step_data = [
            dict(
                step_id=r.step_id, query_id=r.query_id, phase=r.phase, step_index=r.step_index,
                tool_name=r.tool_name, tool_args=r.tool_args, tool_result=r.tool_result, tool_success=r.tool_success,
                tool_error=r.tool_error, summary=r.summary, started_at=r.started_at, finished_at=r.finished_at,
            )
            for r in step_rows
        ]
        llm_rows = (await session.execute(select(AgentLlmCall).where(AgentLlmCall.synced_at.is_(None)))).scalars().all()
        llm_data = [
            dict(
                call_id=r.call_id, step_id=r.step_id, model=r.model, input_tokens=r.input_tokens,
                output_tokens=r.output_tokens, cache_read_tokens=r.cache_read_tokens,
                cache_write_tokens=r.cache_write_tokens, called_at=r.called_at,
            )
            for r in llm_rows
        ]

    if not (query_data or step_data or llm_data):
        return {"queries": 0, "steps": 0, "llm_calls": 0}

    conn = _connect_with_retry()
    try:
        for q in query_data:
            _upsert_query(conn, q)
        for s in step_data:
            _upsert_step(conn, s)
        for l in llm_data:
            _upsert_llm_call(conn, l)
    finally:
        conn.close()

    now = datetime.now(timezone.utc)
    async with async_session_maker() as session:
        for q in query_data:
            await session.execute(update(AgentQuery).where(AgentQuery.query_id == q["query_id"]).values(synced_at=now))
        for s in step_data:
            await session.execute(update(AgentStep).where(AgentStep.step_id == s["step_id"]).values(synced_at=now))
        for l in llm_data:
            await session.execute(update(AgentLlmCall).where(AgentLlmCall.call_id == l["call_id"]).values(synced_at=now))
        await session.commit()

    return {"queries": len(query_data), "steps": len(step_data), "llm_calls": len(llm_data)}


async def main() -> None:
    counts = await sync_once()
    print(f"Synced: {counts}")


if __name__ == "__main__":
    asyncio.run(main())
