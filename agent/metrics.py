"""Metric store -- see docs/dashboard_plan.md.

One entry per metric the dashboard shows. The same entry drives a chart's
"what am I looking at?" popover (GET /dashboard/metrics/{id}) and the
agent's `list_metrics` tool -- so a chart's explanation is written once,
next to the calculation it describes, instead of drifting from it.

Two tiers behind one read interface (get_metric / list_metrics), so callers
never care which tier a metric lives in:
- curated: the tuple below, reviewed in git. The only tier fixed charts use.
- proposed: rows in Postgres (agent/db/dashboard_models.py), written by the
  agent through `create_chart` and shown with an "AI-defined" badge.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from sqlalchemy import select

from agent.db.dashboard_models import MetricRow
from agent.db.postgres import async_session_maker


@dataclass(frozen=True)
class Metric:
    id: str
    name: str
    definition: str  # plain English, for someone new to FPL
    formula: str
    grain: str
    source_views: tuple[str, ...]
    caveats: tuple[str, ...] = ()
    sql_expression: str | None = None  # the SQL fragment an agent should reuse
    status: str = "curated"  # 'curated' | 'proposed' | 'merged' | 'promoted'

    def to_dict(self) -> dict:
        return asdict(self)


_CURATED: tuple[Metric, ...] = (
    Metric(
        id="season_total_points",
        name="Season total points",
        definition=(
            "Fantasy points a player has earned across every gameweek so far this "
            "season. Points come from FPL's scoring rules: appearing, goals, "
            "assists, clean sheets, saves, bonus points, minus cards, own goals "
            "and missed penalties."
        ),
        formula="Sum of a player's FPL points over all gameweeks played this season.",
        grain="one row per player, current season",
        source_views=("v_player_season",),
        caveats=(
            "Only gameweeks already loaded into the warehouse are counted.",
            "A player who moved clubs is shown under their latest club.",
        ),
        sql_expression="v_player_season.total_points",
    ),
    Metric(
        id="gameweek_total_points",
        name="Gameweek points",
        definition=(
            "Fantasy points a player earned in the most recent gameweek, using the "
            "same FPL scoring rules as season points."
        ),
        formula="Sum of a player's FPL points across all of their fixtures in that gameweek.",
        grain="one row per player, latest gameweek",
        source_views=("v_player_gameweek_totals",),
        caveats=(
            "In a double gameweek a player plays twice and both fixtures are summed, "
            "so their score can be roughly double a single-fixture week.",
            "'Latest gameweek' is the highest gameweek with data in the warehouse, "
            "which may lag the real calendar.",
        ),
        sql_expression="v_player_gameweek_totals.total_points WHERE gameweek = (latest gameweek)",
    ),
)

_CURATED_BY_ID = {m.id: m for m in _CURATED}


def name_key(name: str) -> str:
    """Normalized identity of a metric name: 'Goals per 90' == 'goals-per-90'."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _from_row(row: MetricRow) -> Metric:
    return Metric(
        id=row.id,
        name=row.name,
        definition=row.definition,
        formula=row.formula,
        grain=row.grain,
        source_views=tuple(row.source_views or ()),
        caveats=tuple(row.caveats or ()),
        sql_expression=row.sql_expression,
        status=row.status,
    )


async def list_metrics() -> list[Metric]:
    # 'merged' rows are duplicates folded into another metric -- hidden so
    # the agent keeps converging on the canonical one.
    async with async_session_maker() as session:
        rows = (
            await session.scalars(
                select(MetricRow).where(MetricRow.status != "merged").order_by(MetricRow.created_at)
            )
        ).all()
    return [*_CURATED, *(_from_row(r) for r in rows)]


async def get_metric(metric_id: str) -> Metric | None:
    if metric_id in _CURATED_BY_ID:
        return _CURATED_BY_ID[metric_id]
    async with async_session_maker() as session:
        row = await session.get(MetricRow, metric_id)
    return _from_row(row) if row else None


_NEW_FIELDS = ("name", "definition", "formula", "sql_expression", "grain")


def _string_list(value: object) -> list[str]:
    # The model sometimes sends a lone string where a list is expected;
    # iterating that would split it into single characters (found 2026-10-25,
    # when a saved metric's caveats rendered one letter per bullet).
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    raise ValueError("source_views and caveats must be lists of strings.")


async def resolve_metrics(metrics_used: list[dict]) -> tuple[list[dict], list[dict]]:
    """Validate the agent's `metrics_used` for a chart.

    Each entry is `{"metric_id": ...}` (reuse) or `{"new": {...}}` (a
    definition). Returns (resolved, new_definitions): `resolved` is one dict
    per metric with its full definition and `is_new`, ready to show the
    user; `new_definitions` are the ones to persist once the run succeeds.
    Raises ValueError with a message the agent can act on -- crucially, a
    new metric whose name collides with an existing one is rejected with
    that metric's id, which is what steers the agent toward reuse.
    """
    if not metrics_used:
        raise ValueError("metrics_used must list at least one metric (reused or newly defined).")

    existing = await list_metrics()
    by_key = {name_key(m.name): m for m in existing}
    taken_ids = {m.id for m in existing}
    resolved: list[dict] = []
    new_definitions: list[dict] = []
    seen_keys: set[str] = set()

    for entry in metrics_used:
        if "metric_id" in entry:
            metric = await get_metric(entry["metric_id"])
            if metric is None:
                raise ValueError(
                    f"Unknown metric_id {entry['metric_id']!r}. Call list_metrics for valid ids, "
                    "or define a new metric with {'new': {...}}."
                )
            resolved.append({**metric.to_dict(), "is_new": False})
            continue

        new = entry.get("new")
        if not isinstance(new, dict):
            raise ValueError("Each metrics_used entry must be {'metric_id': ...} or {'new': {...}}.")
        missing = [f for f in _NEW_FIELDS if not str(new.get(f, "")).strip()]
        if missing:
            raise ValueError(f"New metric is missing required field(s): {', '.join(missing)}.")

        key = name_key(new["name"])
        if not key:
            raise ValueError("New metric name must contain letters or digits.")
        clash = by_key.get(key)
        if clash is not None:
            raise ValueError(
                f"A metric named {clash.name!r} already exists (metric_id {clash.id!r}). "
                "Reuse it via {'metric_id': ...} instead of redefining it."
            )
        if key in seen_keys:
            raise ValueError(f"Metric {new['name']!r} is defined twice in this chart.")
        seen_keys.add(key)

        metric_id = _slug(new["name"])
        while metric_id in taken_ids:
            metric_id += "_2"
        taken_ids.add(metric_id)
        definition = {
            "id": metric_id,
            "name": new["name"].strip(),
            "name_key": key,
            "definition": new["definition"].strip(),
            "formula": new["formula"].strip(),
            "sql_expression": new["sql_expression"].strip(),
            "grain": new["grain"].strip(),
            "source_views": _string_list(new.get("source_views")),
            "caveats": _string_list(new.get("caveats")),
        }
        new_definitions.append(definition)
        resolved.append(
            {
                **{k: v for k, v in definition.items() if k != "name_key"},
                "source_views": tuple(definition["source_views"]),
                "caveats": tuple(definition["caveats"]),
                "status": "proposed",
                "is_new": True,
            }
        )
    return resolved, new_definitions
