"""Fixed fast-path tools (category A) -- see docs/agent_ui_architecture_plan.md.

First cut based on README's named fantasy decisions (transfers, captaincy,
differentials); expected to evolve once the observability query log has
enough volume to show what's actually asked.

Query only v_player_season / v_player_gameweek_totals (never raw
fact_player_gameweek directly), so double gameweeks always sum correctly --
see CLAUDE.md's data-modeling gotchas. Every function opens its own
short-lived read-only connection (agent.db.duckdb.get_readonly_connection)
and defaults to the current season when one isn't given, since most of
these signatures don't take a season parameter by design (first-cut scope
from the architecture plan).
"""
from __future__ import annotations

from agent.db.duckdb import get_readonly_connection
from agent.tools._common import (
    GAMEWEEK_METRICS,
    SEASON_METRICS,
    current_season_id,
    fetch_dict,
    fetch_dicts,
    validate_metric,
)


def search_players(name_fragment: str, limit: int = 10) -> list[dict]:
    """Resolve a typed name fragment to matching player_codes + display names.

    Entry point most other tools depend on, since users type names, not
    player_code.
    """
    with get_readonly_connection() as conn:
        return fetch_dicts(
            conn,
            """
            SELECT player_code, web_name, first_name, second_name
            FROM dim_player
            WHERE web_name ILIKE '%' || ? || '%'
               OR first_name ILIKE '%' || ? || '%'
               OR second_name ILIKE '%' || ? || '%'
            ORDER BY web_name
            LIMIT ?
            """,
            [name_fragment, name_fragment, name_fragment, limit],
        )


def get_player_summary(player_code: int, season: str | None = None) -> dict:
    """Season totals, price, ownership, form, and position for one player.

    `season` defaults to the current season when omitted.
    """
    with get_readonly_connection() as conn:
        season = season or current_season_id(conn)
        row = fetch_dict(
            conn,
            "SELECT * FROM v_player_season WHERE player_code = ? AND season_id = ?",
            [player_code, season],
        )
    if row is None:
        raise ValueError(f"No data for player_code={player_code} in season={season!r}.")
    return row


def get_player_gameweek_history(
    player_code: int,
    gw_start: int,
    gw_end: int,
    season: str | None = None,
) -> list[dict]:
    """Points/minutes/xG trend for one player over a gameweek range."""
    with get_readonly_connection() as conn:
        season = season or current_season_id(conn)
        return fetch_dicts(
            conn,
            """
            SELECT * FROM v_player_gameweek_totals
            WHERE player_code = ? AND season_id = ? AND gameweek BETWEEN ? AND ?
            ORDER BY gameweek
            """,
            [player_code, season, gw_start, gw_end],
        )


def compare_players(
    player_codes: list[int],
    metric: str,
    gw_start: int | None = None,
    gw_end: int | None = None,
) -> list[dict]:
    """Side-by-side comparison of multiple players on one metric.

    The core "who do I pick" primitive -- `gw_range` omitted means
    season-to-date.
    """
    placeholders = ",".join("?" * len(player_codes))
    with get_readonly_connection() as conn:
        season = current_season_id(conn)
        if gw_start is not None and gw_end is not None:
            metric = validate_metric(metric, GAMEWEEK_METRICS)
            return fetch_dicts(
                conn,
                f"""
                SELECT player_code, web_name, SUM({metric}) AS {metric}
                FROM v_player_gameweek_totals
                WHERE player_code IN ({placeholders}) AND season_id = ? AND gameweek BETWEEN ? AND ?
                GROUP BY player_code, web_name
                ORDER BY {metric} DESC
                """,
                [*player_codes, season, gw_start, gw_end],
            )
        metric = validate_metric(metric, SEASON_METRICS)
        return fetch_dicts(
            conn,
            f"""
            SELECT player_code, web_name, {metric}
            FROM v_player_season
            WHERE player_code IN ({placeholders}) AND season_id = ?
            ORDER BY {metric} DESC
            """,
            [*player_codes, season],
        )


def get_top_performers(
    metric: str,
    gw_start: int,
    gw_end: int,
    position: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Highest performers on one metric over a gameweek range."""
    metric = validate_metric(metric, GAMEWEEK_METRICS)
    with get_readonly_connection() as conn:
        season = current_season_id(conn)
        sql = f"""
            SELECT player_code, web_name, position, team_short_name, SUM({metric}) AS {metric}
            FROM v_player_gameweek_totals
            WHERE season_id = ? AND gameweek BETWEEN ? AND ?
        """
        params: list = [season, gw_start, gw_end]
        if position:
            sql += " AND position = ?"
            params.append(position)
        sql += f" GROUP BY player_code, web_name, position, team_short_name ORDER BY {metric} DESC LIMIT ?"
        params.append(limit)
        return fetch_dicts(conn, sql, params)


def get_differentials(
    max_ownership_pct: float,
    min_form: float,
    position: str | None = None,
) -> list[dict]:
    """Low-ownership, high-form players -- the named "differential" fantasy
    concept, kept as a dedicated tool rather than left to the agent to
    reconstruct the filter logic ad hoc each time.
    """
    with get_readonly_connection() as conn:
        season = current_season_id(conn)
        sql = """
            SELECT player_code, web_name, position, team_short_name,
                   selected_by_percent, form, total_points
            FROM v_player_season
            WHERE season_id = ? AND selected_by_percent <= ? AND form >= ?
        """
        params: list = [season, max_ownership_pct, min_form]
        if position:
            sql += " AND position = ?"
            params.append(position)
        sql += " ORDER BY form DESC"
        return fetch_dicts(conn, sql, params)


def get_value_analysis(
    position: str | None = None,
    player_code: int | None = None,
) -> list[dict]:
    """Points-per-million value, either for one player or a whole position."""
    with get_readonly_connection() as conn:
        season = current_season_id(conn)
        sql = """
            SELECT player_code, web_name, position, team_short_name,
                   total_points, end_price_millions,
                   round(total_points / NULLIF(end_price_millions, 0), 2) AS points_per_million
            FROM v_player_season
            WHERE season_id = ?
        """
        params: list = [season]
        if player_code is not None:
            sql += " AND player_code = ?"
            params.append(player_code)
        elif position is not None:
            sql += " AND position = ?"
            params.append(position)
        sql += " ORDER BY points_per_million DESC"
        return fetch_dicts(conn, sql, params)
