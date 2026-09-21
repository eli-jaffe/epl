"""The shared tool layer's registry -- one list, multiple consumers.

Both the agent loop (agent/loop.py) and the MCP server (agent/mcp_server.py)
build off this same TOOL_REGISTRY instead of each hand-maintaining their own
tool list -- the point of the shared-tool-layer pattern (see References in
docs/agent_ui_architecture_plan.md): don't write tool logic, or a tool's
name/description/schema, twice.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable

from agent.tools import players, sql


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    fn: Callable[..., Any]
    input_schema: dict = field(default_factory=dict)


TOOL_REGISTRY: list[ToolSpec] = [
    ToolSpec(
        "search_players",
        "Resolve a name fragment to matching player_codes.",
        players.search_players,
        {
            "type": "object",
            "properties": {
                "name_fragment": {"type": "string", "description": "Full or partial player name as typed by the user."},
                "limit": {"type": "integer", "description": "Max matches to return.", "default": 10},
            },
            "required": ["name_fragment"],
        },
    ),
    ToolSpec(
        "get_player_summary",
        "Season totals, price, ownership, form, and position for one player.",
        players.get_player_summary,
        {
            "type": "object",
            "properties": {
                "player_code": {"type": "integer", "description": "Stable FPL player_code from search_players, not the season-scoped id."},
                "season": {"type": "string", "description": "e.g. '2025-26'. Defaults to the current season if omitted."},
            },
            "required": ["player_code"],
        },
    ),
    ToolSpec(
        "get_player_gameweek_history",
        "Points/minutes/xG trend for one player over a gameweek range.",
        players.get_player_gameweek_history,
        {
            "type": "object",
            "properties": {
                "player_code": {"type": "integer", "description": "Stable FPL player_code from search_players."},
                "gw_start": {"type": "integer", "description": "First gameweek in range, inclusive."},
                "gw_end": {"type": "integer", "description": "Last gameweek in range, inclusive."},
                "season": {"type": "string", "description": "e.g. '2025-26'. Defaults to the current season if omitted."},
            },
            "required": ["player_code", "gw_start", "gw_end"],
        },
    ),
    ToolSpec(
        "compare_players",
        "Side-by-side comparison of multiple players on one metric.",
        players.compare_players,
        {
            "type": "object",
            "properties": {
                "player_codes": {"type": "array", "items": {"type": "integer"}, "description": "Stable FPL player_codes from search_players."},
                "metric": {"type": "string", "description": "e.g. 'total_points', 'expected_goals', 'form'."},
                "gw_start": {"type": "integer", "description": "First gameweek in range, inclusive. Omit for season-to-date."},
                "gw_end": {"type": "integer", "description": "Last gameweek in range, inclusive. Omit for season-to-date."},
            },
            "required": ["player_codes", "metric"],
        },
    ),
    ToolSpec(
        "get_top_performers",
        "Highest performers on one metric over a gameweek range.",
        players.get_top_performers,
        {
            "type": "object",
            "properties": {
                "metric": {"type": "string", "description": "e.g. 'total_points', 'expected_goals', 'bonus'."},
                "gw_start": {"type": "integer", "description": "First gameweek in range, inclusive."},
                "gw_end": {"type": "integer", "description": "Last gameweek in range, inclusive."},
                "position": {"type": "string", "enum": ["GK", "DEF", "MID", "FWD"], "description": "Restrict to one position. Omit for all positions."},
                "limit": {"type": "integer", "description": "Max players to return.", "default": 10},
            },
            "required": ["metric", "gw_start", "gw_end"],
        },
    ),
    ToolSpec(
        "get_differentials",
        "Low-ownership, high-form players.",
        players.get_differentials,
        {
            "type": "object",
            "properties": {
                "max_ownership_pct": {"type": "number", "description": "Upper bound on selected_by_percent, e.g. 5.0 for under 5%."},
                "min_form": {"type": "number", "description": "Lower bound on the form metric."},
                "position": {"type": "string", "enum": ["GK", "DEF", "MID", "FWD"], "description": "Restrict to one position. Omit for all positions."},
            },
            "required": ["max_ownership_pct", "min_form"],
        },
    ),
    # get_fixture_difficulty (single-team-per-call) deliberately removed
    # 2026-09-20: answering "which teams" style questions needs it called
    # once per club, and burned most of the agent loop's execute-round
    # budget on schema rediscovery before ever reaching it -- see the known
    # issue in docs/agent_ui_architecture_plan.md. Deferred as a v2 fixed
    # tool (something like get_fixture_difficulty_all_teams); for now,
    # cross-team fixture questions go through run_sql against fact_fixture
    # directly, which the agent already reaches for as an escape hatch.
    ToolSpec(
        "get_value_analysis",
        "Points-per-million value, for one player or a whole position.",
        players.get_value_analysis,
        {
            "type": "object",
            "properties": {
                "position": {"type": "string", "enum": ["GK", "DEF", "MID", "FWD"], "description": "Analyze a whole position. Omit if player_code is given."},
                "player_code": {"type": "integer", "description": "Analyze one player. Omit if position is given."},
            },
            "required": [],
        },
    ),
    ToolSpec(
        "get_schema",
        "DuckDB warehouse schema plus per-column profiling, for NL2SQL grounding.",
        sql.get_schema,
        {"type": "object", "properties": {}, "required": []},
    ),
    ToolSpec(
        "run_sql",
        "Execute a read-only SELECT against the DuckDB warehouse.",
        sql.run_sql,
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "A single read-only SELECT statement."},
                "row_limit": {"type": "integer", "description": "Max rows to return.", "default": 500},
            },
            "required": ["query"],
        },
    ),
]

TOOLS_BY_NAME: dict[str, ToolSpec] = {tool.name: tool for tool in TOOL_REGISTRY}


def to_claude_tools(names: list[str] | None = None) -> list[dict]:
    """TOOL_REGISTRY (or a subset, by name) in the shape Claude's `tools=`
    API parameter expects.
    """
    specs = TOOL_REGISTRY if names is None else [TOOLS_BY_NAME[n] for n in names]
    return [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in specs
    ]


async def dispatch(name: str, **kwargs: Any) -> dict:
    """Run a registered tool by name and return a uniform envelope --
    {"success", "data", "error"} -- so callers never branch on tool-specific
    exception types (the tool-envelope pattern from
    references/agentic_analyst_review_usmnt.md).

    Tool functions are sync (they'll do blocking DuckDB calls); running via
    asyncio.to_thread keeps that off the event loop. Any exception the tool
    raises -- including today's expected NotImplementedError, since the
    tools themselves aren't implemented yet -- is caught here and reported
    as a failed envelope rather than propagating.
    """
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return {"success": False, "data": None, "error": f"Unknown tool: {name}"}
    try:
        data = await asyncio.to_thread(tool.fn, **kwargs)
        return {"success": True, "data": data, "error": None}
    except Exception as e:  # noqa: BLE001 -- deliberate: see docstring
        return {"success": False, "data": None, "error": f"{type(e).__name__}: {e}"}
