"""Postgres models for agent observability -- the live write target.

See docs/agent_ui_architecture_plan.md and
.claude/plans/vast-hopping-origami.md for why: the live app needs to stay
strictly read-only against db/epl.duckdb (so run_sql's driver-level
read-only safety layer is real, not just an app-level blocklist), so
observability writes go here instead, on the same self-hosted Postgres
already open for auth/app-state. agent/observability_sync.py periodically
copies unsynced rows into DuckDB's matching agent_query/agent_step/
agent_llm_call tables (db/schema.sql) for query-pattern-mining/cost
analysis -- Postgres is the live source of truth, DuckDB is a synced copy
that can lag by up to the sync interval.

Mirrors the DuckDB table shapes, plus a nullable `synced_at` on each table
(NULL = not yet copied to DuckDB).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Uuid
from sqlalchemy.orm import Mapped, mapped_column

# All timestamps written here are timezone-aware (agent/observability.py's
# _now() uses datetime.now(timezone.utc)). Without timezone=True, SQLAlchemy
# maps `datetime` to TIMESTAMP WITHOUT TIME ZONE by default, and asyncpg
# refuses to encode an aware datetime into a naive column -- found by
# actually writing to Postgres, 2026-09-18 (not caught by import-only checks).
_TZDateTime = DateTime(timezone=True)

# AgentQuery.user_id below has a ForeignKey to "user.id", but nothing else
# in this module's import chain ever imports agent.auth.models -- without
# this, SQLAlchemy doesn't know the `user` table exists yet when it tries
# to resolve that FK (NoReferencedTableError), unless some *other* already-
# imported module happened to import agent.auth.models first. Confirmed
# this bites agent/observability_sync.py when run standalone (its own
# import chain never touches agent.auth.models either) -- found by actually
# running the agent loop end-to-end, 2026-09-18.
from agent.auth import models as _auth_models  # noqa: F401
from agent.db.postgres import Base


class AgentQuery(Base):
    __tablename__ = "agent_query"

    query_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user.id"), nullable=False)
    query_text: Mapped[str] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(nullable=False)  # 'running' | 'success' | 'failed'
    started_at: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(_TZDateTime, default=None)
    final_answer: Mapped[str | None] = mapped_column(default=None)
    synced_at: Mapped[datetime | None] = mapped_column(_TZDateTime, default=None)


class AgentStep(Base):
    __tablename__ = "agent_step"

    step_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    query_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_query.query_id"), nullable=False)
    phase: Mapped[str] = mapped_column(nullable=False)
    step_index: Mapped[int] = mapped_column(nullable=False)
    tool_name: Mapped[str | None] = mapped_column(default=None)
    tool_args: Mapped[dict | None] = mapped_column(JSON, default=None)
    tool_result: Mapped[str | None] = mapped_column(default=None)  # JSON-encoded, truncated; only set on success
    tool_success: Mapped[bool | None] = mapped_column(default=None)
    tool_error: Mapped[str | None] = mapped_column(default=None)
    summary: Mapped[str | None] = mapped_column(default=None)
    started_at: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(_TZDateTime, default=None)
    synced_at: Mapped[datetime | None] = mapped_column(_TZDateTime, default=None)


class AgentLlmCall(Base):
    __tablename__ = "agent_llm_call"

    call_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    step_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_step.step_id"), nullable=False)
    model: Mapped[str] = mapped_column(nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(default=None)
    output_tokens: Mapped[int | None] = mapped_column(default=None)
    cache_read_tokens: Mapped[int | None] = mapped_column(default=None)
    cache_write_tokens: Mapped[int | None] = mapped_column(default=None)
    # No cost_usd here -- computed at sync time against DuckDB's
    # agent_model_pricing, not on the hot path. See agent/observability_sync.py.
    called_at: Mapped[datetime] = mapped_column(_TZDateTime, nullable=False)
    synced_at: Mapped[datetime | None] = mapped_column(_TZDateTime, default=None)
