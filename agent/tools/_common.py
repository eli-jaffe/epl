"""Shared helpers for the tool layer -- agent/tools/players.py, sql.py."""
from __future__ import annotations

import duckdb


def current_season_id(conn: duckdb.DuckDBPyConnection) -> str:
    row = conn.execute("SELECT season_id FROM dim_season WHERE is_current").fetchone()
    if row is None:
        raise RuntimeError("No season is marked is_current in dim_season.")
    return row[0]


def fetch_dicts(conn: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> list[dict]:
    result = conn.execute(sql, params or [])
    columns = [d[0] for d in result.description]
    return [dict(zip(columns, row)) for row in result.fetchall()]


def fetch_dict(conn: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> dict | None:
    rows = fetch_dicts(conn, sql, params)
    return rows[0] if rows else None


# metric names are LLM-supplied and get interpolated into SQL as column
# identifiers (DuckDB can't parameterize identifiers, only values) -- these
# allowlists are the only thing standing between that and SQL injection via
# a crafted "metric" argument, so every f-string SQL build below validates
# through here first. Two separate lists because v_player_season and
# v_player_gameweek_totals don't share every column.
SEASON_METRICS = {"total_points", "minutes", "selected_by_percent", "points_per_game", "form"}
GAMEWEEK_METRICS = {
    "total_points", "minutes", "goals_scored", "assists", "clean_sheets",
    "goals_conceded", "bonus", "bps", "expected_goals", "expected_assists",
    "yellow_cards", "red_cards",
}


def validate_metric(metric: str, allowed: set[str]) -> str:
    if metric not in allowed:
        raise ValueError(f"Unknown metric {metric!r}. Valid options: {sorted(allowed)}")
    return metric
