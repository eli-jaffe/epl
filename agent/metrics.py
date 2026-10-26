"""Metric store -- see docs/dashboard_plan.md.

One entry per metric the dashboard shows. The same entry drives a chart's
"what am I looking at?" popover (GET /dashboard/metrics/{id}) and, later,
`list_metrics` for the agent -- so a chart's explanation is written once,
next to the calculation it describes, instead of drifting from it.

Phase 1 is the curated tier only (this module, reviewed in git). Agent-
defined `proposed` metrics get a Postgres-backed second tier in phase 3,
behind the same get_metric/list_metrics interface, so callers don't care
which tier a metric lives in.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Metric:
    id: str
    name: str
    definition: str  # plain English, for someone new to FPL
    formula: str
    grain: str
    source_views: tuple[str, ...]
    caveats: tuple[str, ...] = ()
    status: str = "curated"  # 'curated' | 'proposed' (phase 3)

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
    ),
)

_BY_ID = {m.id: m for m in _CURATED}


def get_metric(metric_id: str) -> Metric | None:
    return _BY_ID.get(metric_id)


def list_metrics() -> list[Metric]:
    return list(_CURATED)
