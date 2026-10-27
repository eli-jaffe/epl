"""Dashboard API -- the fixed charts' data, metric explanations, and the
agent-generated chart endpoint, see docs/dashboard_plan.md. The fixed-chart
routes are plain parameterized SQL over the consumption views, no LLM.

Fixed-chart routes are sync `def` (FastAPI runs them in its threadpool)
because the DuckDB calls block; the agent tools do the same. The metric and
chart routes are async because the metric store reads Postgres.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select

from agent import charts, metrics
from agent.api.chat import _stream_answer
from agent.auth.models import User
from agent.auth.users import current_active_user, current_superuser
from agent.db.dashboard_models import ChartRequest, ChartRequestMetric, MetricRow
from agent.db.duckdb import get_readonly_connection
from agent.db.postgres import async_session_maker
from agent.loop import OnStatus, run_agent_query
from agent.run_context import RunContext
from agent.tools import charts as chart_tools
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
async def get_metrics(user: User = Depends(current_active_user)) -> list[dict]:
    return [m.to_dict() for m in await metrics.list_metrics()]


@router.get("/metrics/{metric_id}")
async def get_metric_by_id(metric_id: str, user: User = Depends(current_active_user)) -> dict:
    metric = await metrics.get_metric(metric_id)
    if metric is None:
        raise HTTPException(status_code=404, detail="Metric not found")
    return metric.to_dict()


class ChartRequestBody(BaseModel):
    query: str
    conversation_id: uuid.UUID | None = None


@router.post("/chart")
async def create_chart_from_prompt(
    body: ChartRequestBody, user: User = Depends(current_active_user)
) -> StreamingResponse:
    """Same SSE shape as /chat (status frames, then one terminal frame), but
    the answer frame also carries `chart` -- null if the agent answered in
    text without producing one."""
    run = RunContext(user_id=user.id, chart_mode=True)
    payload: dict = {}

    async def run_query(on_status: OnStatus) -> str:
        answer = await run_agent_query(
            user.id, body.query, on_status=on_status, conversation_id=body.conversation_id, run_ctx=run
        )
        # Saved here, after the loop settled on its final chart -- see
        # agent/tools/charts.py for why not inside the tool.
        if run.chart is not None:
            chart_id = await charts.save_chart(run, body.query)
            payload["chart"] = {
                "id": str(chart_id),
                "title": run.chart["title"],
                "sql": run.chart["sql"],
                "spec": run.chart["spec"],
                "metrics": run.chart["metrics"],
            }
        return answer

    return StreamingResponse(
        _stream_answer(run_query, extra=lambda: {"chart": payload.get("chart")}),
        media_type="text/event-stream",
    )


# ---- My charts -------------------------------------------------------------


@router.get("/charts")
async def list_my_charts(user: User = Depends(current_active_user)) -> list[dict]:
    """The caller's saved charts, newest first. No specs -- those carry up to
    500 rows each, so the list stays light and a chart is fetched on open."""
    async with async_session_maker() as session:
        rows = (
            await session.execute(
                select(ChartRequest.id, ChartRequest.title, ChartRequest.prompt, ChartRequest.created_at)
                .where(ChartRequest.user_id == user.id)
                .order_by(ChartRequest.created_at.desc())
                .limit(50)
            )
        ).all()
    return [
        {"id": str(r.id), "title": r.title, "prompt": r.prompt, "created_at": r.created_at.isoformat()}
        for r in rows
    ]


@router.get("/charts/{chart_id}")
async def get_my_chart(chart_id: uuid.UUID, user: User = Depends(current_active_user)) -> dict:
    async with async_session_maker() as session:
        chart = await session.get(ChartRequest, chart_id)
        # Someone else's chart is a 404, not a 403: don't confirm it exists.
        if chart is None or chart.user_id != user.id:
            raise HTTPException(status_code=404, detail="Chart not found")
        links = (
            await session.scalars(
                select(ChartRequestMetric).where(ChartRequestMetric.chart_request_id == chart_id)
            )
        ).all()
    chart_metrics = []
    for link in links:
        metric = await metrics.get_metric(link.metric_id)
        if metric is not None:
            chart_metrics.append({**metric.to_dict(), "is_new": link.role == "defined"})
    return {
        "id": str(chart.id),
        "title": chart.title,
        "sql": chart.sql,
        "spec": chart.spec,
        "metrics": chart_metrics,
        "created_at": chart.created_at.isoformat(),
    }


@router.post("/charts/{chart_id}/refresh")
async def refresh_my_chart(chart_id: uuid.UUID, user: User = Depends(current_active_user)) -> dict:
    """Re-run a saved chart's SQL against the current warehouse and return the
    spec with the new rows. Nothing is written: the saved chart stays the
    snapshot from when it was created, and a refresh is a look at today."""
    async with async_session_maker() as session:
        chart = await session.get(ChartRequest, chart_id)
        if chart is None or chart.user_id != user.id:
            raise HTTPException(status_code=404, detail="Chart not found")
        sql, spec = chart.sql, chart.spec
    try:
        fresh_spec, _ = await chart_tools.with_fresh_rows(spec, sql)
    except Exception as e:  # noqa: BLE001 -- surface sandbox/SQL errors to the user
        raise HTTPException(status_code=422, detail=f"Couldn't refresh this chart: {e}")
    return {"spec": fresh_spec, "refreshed_at": datetime.now(timezone.utc).isoformat()}


# ---- Admin: metrics library -----------------------------------------------
# Superuser-only (promote an account with `python -m agent.auth.promote_admin`).
# Lives under /dashboard/admin rather than /admin so it doesn't collide with
# the frontend's /admin/* page routes in the dev proxy.


def _root_of(metric_id: str, merged_into: dict[str, str]) -> str:
    # Merges form chains (A -> B -> C) rather than being flattened, so an
    # un-merge restores exactly what was there. Cycles can't form: a merge
    # target must itself be unmerged.
    while metric_id in merged_into:
        metric_id = merged_into[metric_id]
    return metric_id


@router.get("/admin/metrics")
async def admin_list_metrics(user: User = Depends(current_superuser)) -> list[dict]:
    """Every metric with how often charts have used it. A merged metric's
    requests count toward the metric it was merged into."""
    async with async_session_maker() as session:
        rows = (await session.scalars(select(MetricRow).order_by(MetricRow.created_at))).all()
        links = (
            await session.execute(
                select(ChartRequestMetric.metric_id, ChartRequestMetric.role, func.count())
                .group_by(ChartRequestMetric.metric_id, ChartRequestMetric.role)
            )
        ).all()

    merged_into = {r.id: r.merged_into for r in rows if r.status == "merged" and r.merged_into}
    own: dict[str, dict[str, int]] = {}
    rolled: dict[str, int] = {}
    for metric_id, role, n in links:
        own.setdefault(metric_id, {"defined": 0, "reused": 0})[role] += n
        root = _root_of(metric_id, merged_into)
        rolled[root] = rolled.get(root, 0) + n

    merged_children: dict[str, list[str]] = {}
    for child in merged_into:
        merged_children.setdefault(_root_of(child, merged_into), []).append(child)

    out = []
    for metric in [*metrics.curated_metrics(), *(metrics.from_row(r) for r in rows if r.id not in metrics.curated_ids())]:
        out.append(
            {
                **metric.to_dict(),
                "requests": rolled.get(metric.id, 0),
                "own": own.get(metric.id, {"defined": 0, "reused": 0}),
                "merged_from": merged_children.get(metric.id, []),
                "merged_into": merged_into.get(metric.id),
                "snippet": metrics.curated_snippet(metric) if metric.status == "promoted" else None,
            }
        )
    out.sort(key=lambda m: (-m["requests"], m["name"]))
    return out


class MergeBody(BaseModel):
    into: str


@router.post("/admin/metrics/{metric_id}/merge")
async def admin_merge_metric(
    metric_id: str, body: MergeBody, user: User = Depends(current_superuser)
) -> dict:
    target = await metrics.get_metric(body.into)
    async with async_session_maker() as session:
        row = await session.get(MetricRow, metric_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Only agent-defined metrics can be merged")
        if row.status != "proposed":
            raise HTTPException(status_code=409, detail=f"A {row.status} metric can't be merged")
        if body.into == metric_id or target is None:
            raise HTTPException(status_code=400, detail="Pick a different, existing metric to merge into")
        if target.status == "merged":
            raise HTTPException(status_code=409, detail="That metric is itself merged; pick the one it points to")
        row.status = "merged"
        row.merged_into = body.into
        await session.commit()
    return {"ok": True}


@router.post("/admin/metrics/{metric_id}/unmerge")
async def admin_unmerge_metric(metric_id: str, user: User = Depends(current_superuser)) -> dict:
    async with async_session_maker() as session:
        row = await session.get(MetricRow, metric_id)
        if row is None or row.status != "merged":
            raise HTTPException(status_code=404, detail="No merged metric with that id")
        row.status = "proposed"
        row.merged_into = None
        await session.commit()
    return {"ok": True}


@router.post("/admin/metrics/{metric_id}/promote")
async def admin_promote_metric(metric_id: str, user: User = Depends(current_superuser)) -> dict:
    """Mark a metric for the curated tier and return the `Metric(...)` source to
    paste into agent/metrics.py -- curated metrics are reviewed in git, so this
    never edits code itself."""
    async with async_session_maker() as session:
        row = await session.get(MetricRow, metric_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Only agent-defined metrics can be promoted")
        if row.status != "proposed":
            raise HTTPException(status_code=409, detail=f"A {row.status} metric can't be promoted")
        row.status = "promoted"
        await session.commit()
        snippet = metrics.curated_snippet(metrics.from_row(row))
    return {"ok": True, "snippet": snippet}
