"""SQL escape hatch (category B) -- see docs/agent_ui_architecture_plan.md.

run_sql() implements three of its four originally-committed safety layers:
single-SELECT-statement enforcement + a keyword blocklist, a hard row cap,
and a wall-clock timeout (DuckDB has no native one, so this runs the query
in a thread and calls conn.interrupt() -- confirmed empirically 2026-09-18
that this actually cancels an in-progress query, not just a documented-but-
unverified API). The fourth, per-session rate limiting, is deliberately not
here: it needs user/session context that doesn't currently reach the tool
layer at all (dispatch() calls every tool with exactly what the LLM
supplied, nothing else), which is an architecture decision -- not implicit
in "write the function" -- left for a follow-up.

get_readonly_connection() (agent/db/duckdb.py) is already read_only=True,
so run_sql actually gets two independent layers: the blocklist below (fast,
clear error, before touching the DB) and DuckDB's own engine-level refusal
of any write that somehow got past it.
"""
from __future__ import annotations

import re
import threading

import duckdb

from agent.db.duckdb import get_readonly_connection
from agent.tools._common import current_season_id

# The views run_sql/agents should be steered toward -- not the raw fact
# tables, since those are where the double-gameweek/`id`-vs-`code` footguns
# live (see CLAUDE.md's data-modeling gotchas). get_schema only describes
# these three, deliberately, consistent with the README's own framing of
# them as "what the LLM agent and frontend will query."
_GROUNDING_VIEWS = ["v_player_season", "v_player_gameweek_totals", "v_player_gameweek"]


def get_schema() -> dict:
    """Current season, its available gameweek range, and column/type info
    for the consumption views -- NL2SQL grounding.

    Simplified from the original per-column-profiling ambition (distinct
    counts/null%/sample values) to just columns+types for this first pass --
    that richer version is real follow-up work, not dropped silently, if
    grounding ever proves insufficient without it.
    """
    with get_readonly_connection() as conn:
        season = current_season_id(conn)
        gw_min, gw_max = conn.execute(
            "SELECT min(gameweek), max(gameweek) FROM v_player_gameweek_totals WHERE season_id = ?",
            [season],
        ).fetchone()
        views = {
            view: [
                {"column": row[0], "type": row[1]}
                for row in conn.execute(f"DESCRIBE {view}").fetchall()
            ]
            for view in _GROUNDING_VIEWS
        }
    return {
        "current_season": season,
        "current_season_gameweek_range": {"min": gw_min, "max": gw_max},
        "views": views,
    }


MAX_ROW_LIMIT = 2000
QUERY_TIMEOUT_SECONDS = 10.0

# Word-boundary match, not substring -- so a column comment mentioning
# "delete" doesn't false-positive, but "DELETE FROM x" does. Deliberately
# broader than ANSI DML/DDL: includes DuckDB-specific escape hatches
# (ATTACH/DETACH/INSTALL/LOAD/PRAGMA/COPY/EXPORT/IMPORT) an LLM could
# otherwise reach for that a generic SQL blocklist would miss.
_BLOCKED_KEYWORDS = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|ATTACH|"
    r"DETACH|COPY|EXPORT|IMPORT|PRAGMA|CALL|INSTALL|LOAD|SET|VACUUM|"
    r"CHECKPOINT|GRANT|REVOKE|MERGE)\b",
    re.IGNORECASE,
)


def _validate_select_only(query: str) -> str:
    statements = [s for s in query.strip().rstrip(";").split(";") if s.strip()]
    if len(statements) != 1:
        raise ValueError("run_sql accepts exactly one SQL statement, not multiple.")
    statement = statements[0].strip()
    if not statement.upper().startswith("SELECT"):
        raise ValueError("run_sql only accepts a single read-only SELECT statement.")
    if _BLOCKED_KEYWORDS.search(statement):
        raise ValueError(
            "Query contains a disallowed keyword -- only read-only SELECT "
            "statements are permitted."
        )
    return statement


def _execute_with_timeout(
    conn: duckdb.DuckDBPyConnection, statement: str, timeout_seconds: float
) -> duckdb.DuckDBPyConnection:
    outcome: dict = {}

    def target() -> None:
        try:
            outcome["result"] = conn.execute(statement)
        except Exception as e:  # includes duckdb.InterruptException on cancel
            outcome["error"] = e

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)
    if thread.is_alive():
        conn.interrupt()
        thread.join(timeout=5.0)
        raise TimeoutError(f"Query exceeded the {timeout_seconds:.0f}s timeout and was interrupted.")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]


def run_sql(query: str, row_limit: int = 500) -> list[dict]:
    """Execute a single read-only SELECT against the DuckDB warehouse.

    See the module docstring for the safety layers this enforces, and which
    one (per-session rate limiting) it deliberately does not.
    """
    statement = _validate_select_only(query)
    row_limit = max(1, min(row_limit, MAX_ROW_LIMIT))
    with get_readonly_connection() as conn:
        result = _execute_with_timeout(conn, statement, QUERY_TIMEOUT_SECONDS)
        columns = [d[0] for d in result.description]
        rows = result.fetchmany(row_limit)
    return [dict(zip(columns, row)) for row in rows]
