"""Per-run context for the agent loop -- who is asking, and (in chart mode)
the chart the run produced.

Tools get exactly what the LLM supplied and nothing else (see
agent/tools/registry.py's dispatch), so they have no way to know the user or
query. A ContextVar reaches them without changing any tool signature, and it
survives asyncio.to_thread (which copies the current context). The endpoint
creates the RunContext, passes it to run_agent_query, and reads `.chart`
back afterwards -- same object, so no return-type change to the loop.
"""
from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class RunContext:
    user_id: uuid.UUID
    chart_mode: bool = False
    query_id: uuid.UUID | None = None  # set by run_agent_query once the query row exists
    # Last successful create_chart call. A retry of the loop's Execute phase
    # overwrites it, so only the final attempt's chart is ever saved.
    chart: dict | None = None


current_run: ContextVar[RunContext | None] = ContextVar("current_run", default=None)
