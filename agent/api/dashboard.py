"""Dashboard API -- the fixed charts' data and metric explanations, see
docs/dashboard_plan.md. No LLM involved: plain parameterized SQL over the
consumption views, so these are cheap and deterministic.

Routes are sync `def` (FastAPI runs them in its threadpool) because the
DuckDB calls block; the agent tools do the same.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from agent.auth.models import User
from agent.auth.users import current_active_user
from agent.db.duckdb import get_readonly_connection
from agent.metrics import get_metric, list_metrics
from agent.tools._common import current_season_id, fetch_dicts

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

Position = Literal["GK", "DEF", "MID", "FWD"]
POSITIONS: list[str] = ["GK", "DEF", "MID", "FWD"]


@router.get("/filters")
def get_filters(user: User = Depends(current_active_user)) -> dict:
    with get_readonly_connection() as conn:
        season = current_season_id(conn)
        # Keyed on the season's own short_name, not a global name -- a club's
        # display name can change between stints (CLAUDE.md gotcha).
        teams = fetch_dicts(
            conn,
            """
            SELECT DISTINCT team_short_name AS short_name, team_name AS name
            FROM v_player_season
            WHERE season_id = ? AND team_short_name IS NOT NULL
            ORDER BY team_name
            """,
            [season],
        )
        latest_gw = conn.execute(
            "SELECT max(gameweek) FROM v_player_gameweek_totals WHERE season_id = ?", [season]
        ).fetchone()[0]
    return {"season_id": season, "latest_gameweek": latest_gw, "teams": teams, "positions": POSITIONS}


@router.get("/top-players")
def get_top_players(
    scope: Literal["season", "gameweek"],
    team: str | None = None,
    position: Position | None = None,
    limit: int = Query(10, ge=1, le=50),
    user: User = Depends(current_active_user),
) -> dict:
    where = ["season_id = ?"]
    with get_readonly_connection() as conn:
        season = current_season_id(conn)
        params: list = [season]
        gameweek = None
        if scope == "gameweek":
            gameweek = conn.execute(
                "SELECT max(gameweek) FROM v_player_gameweek_totals WHERE season_id = ?", [season]
            ).fetchone()[0]
            if gameweek is None:
                raise HTTPException(status_code=404, detail="No gameweek data loaded for the current season")
            where.append("gameweek = ?")
            params.append(gameweek)
        if team:
            where.append("team_short_name = ?")
            params.append(team)
        if position:
            where.append("position = ?")
            params.append(position)

        if scope == "season":
            metric_id = "season_total_points"
            select = "player_code, web_name, position, team_short_name, total_points, minutes"
            source = "v_player_season"
        else:
            metric_id = "gameweek_total_points"
            select = (
                "player_code, web_name, position, team_short_name, total_points, minutes, "
                "goals_scored, assists, bonus, opponents"
            )
            source = "v_player_gameweek_totals"

        # web_name tiebreak keeps the ordering stable across refetches.
        players = fetch_dicts(
            conn,
            f"SELECT {select} FROM {source} WHERE {' AND '.join(where)} "
            "ORDER BY total_points DESC, minutes ASC, web_name LIMIT ?",
            params + [limit],
        )
    return {"scope": scope, "season_id": season, "gameweek": gameweek, "metric_id": metric_id, "players": players}


@router.get("/metrics")
def get_metrics(user: User = Depends(current_active_user)) -> list[dict]:
    return [m.to_dict() for m in list_metrics()]


@router.get("/metrics/{metric_id}")
def get_metric_by_id(metric_id: str, user: User = Depends(current_active_user)) -> dict:
    metric = get_metric(metric_id)
    if metric is None:
        raise HTTPException(status_code=404, detail="Metric not found")
    return metric.to_dict()
