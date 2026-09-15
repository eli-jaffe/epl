"""Load the raw GitHub-repo CSVs (landing zone) into the conformed DuckDB schema.

Known source-data realities this script works around (discovered by inspecting
the actual files, not assumed):
  - `code` is the stable identity for players/teams across seasons; `id` is
    reassigned every season. gws/fixtures files reference `id`, so each
    season's own players_raw.csv/teams.csv is used as the (season, id) -> code
    crosswalk.
  - teams.csv is missing entirely for 2016-17/2017-18/2018-19. We fall back to
    the repo's master_team_list.csv (season, id, team_name) and resolve
    team_name against the name->code index built from seasons that DO have
    teams.csv. A handful of clubs relegated before 2019-20 and never promoted
    back since (e.g. Hull, Middlesbrough, Stoke, Swansea, Huddersfield,
    Cardiff) never appear in any teams.csv we have, so their code stays
    unresolved — a real, bounded gap in the source data, logged not hidden.
  - gws/merged_gw.csv schema drifts by season: 2016-17-2018-19 use a legacy
    column set (no `team`/`position`, different advanced-stat names), 2019-20
    is transitional, 2020-21+ is the "modern" schema, and 2025-26+ adds
    defensive-contribution stats (tackles, recoveries, etc). Columns are read
    defensively — missing ones become NULL rather than raising.
  - merged_gw.csv occasionally contains exact duplicate rows (same element,
    fixture, GW — verified by inspecting the raw data, not a double-gameweek
    since fixture_id is identical) — deduped before loading.
  - A player can have two rows for the same (season, gameweek) when their
    team has a double gameweek — that's why fact_player_gameweek's grain is
    per-fixture (season, gameweek, player_code, fixture_id), not strictly
    per-gameweek. Use v_player_gameweek_totals for weekly-summed totals.

This script does a full delete+reload of the historical tables each run
(the conformed warehouse is treated as disposable/rebuildable), and appends
one row to ingest_runs per execution for lineage.

The ~254K-row gameweek table is built with vectorized pandas operations
(column-wise .map()/.astype(), no Python-level row loop) rather than
iterrows() — iterrows() boxes every row into a Series and was the reason an
earlier version of this script took several minutes; the vectorized version
runs in low single-digit seconds. Loading into DuckDB uses `INSERT ... BY
NAME SELECT * FROM <dataframe>`, which lets DuckDB's own vectorized engine
do the insert and matches columns by name instead of position — this also
eliminates an earlier class of bug where hand-counted `?` placeholders
didn't match the actual column count.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

import config

DB_PATH = Path(__file__).resolve().parent.parent / "db" / "epl.duckdb"

POSITION_MAP = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD", 5: "AM"}
SOURCE_NAME = "github_repo"


def read_csv_safe(path: Path):
    if not path.exists():
        return None
    try:
        return pd.read_csv(path)
    except UnicodeDecodeError:
        return pd.read_csv(path, encoding="latin-1")


def season_dir(season: str) -> Path:
    return config.RAW_DATA_DIR / season


def load_manifest() -> dict:
    return json.loads((config.RAW_DATA_DIR / "manifest.json").read_text())


def nn(value):
    """Convert pandas/numpy NaN-ish scalars to None for DB insertion."""
    if value is None:
        return None
    if isinstance(value, float) and np.isnan(value):
        return None
    if pd.isna(value):
        return None
    return value


# ---------------------------------------------------------------------------
# Team crosswalk (small data — teams.csv is ~20 rows/season — plain loops fine)
# ---------------------------------------------------------------------------
def build_team_crosswalk(seasons):
    """NOTE: a club's `code` is stable across its stints in the league, but its
    display `name` is not — e.g. Ipswich's code (40) was unchanged between
    2024-25 ("Ipswich") and 2026-27 ("Ipswich Town"), a real FPL naming
    change, not a data error. A single global name->code index would keep
    only the newest name per code and silently fail to resolve the `team`
    column in older seasons' gw files. So we keep both: `season_name_to_code`
    (that season's own teams.csv name->code — used to resolve that season's
    own gw files) and a global `name_to_code` (last-name-wins — used only as
    the master_team_list.csv fallback for seasons with no teams.csv at all,
    where no season-specific mapping exists to prefer)."""
    team_records = {}          # code -> {name, short_name}
    id_to_code = {s: {} for s in seasons}
    season_name_to_code = {s: {} for s in seasons}
    unresolved = []            # (season, id, name) that couldn't be resolved

    for season in seasons:
        df = read_csv_safe(season_dir(season) / "teams.csv")
        if df is None:
            continue
        for _, row in df.iterrows():
            code = int(row["code"])
            id_to_code[season][int(row["id"])] = code
            season_name_to_code[season][row["name"]] = code
            team_records[code] = {"name": row["name"], "short_name": row["short_name"]}

    name_to_code = {rec["name"]: code for code, rec in team_records.items()}
    # Fallback for when the exact name doesn't match: a club can be known by a
    # short and a full name at different points (Hull / Hull City, Ipswich /
    # Ipswich Town), and master_team_list.csv's short-form names sometimes
    # only line up with a *different* season's teams.csv than the one it's
    # resolving. Normalizing away common club-name suffixes lets "Hull"
    # (master_team_list, 2016-17) match "Hull City" (only ever seen in
    # 2026-27's teams.csv) without hardcoding per-club aliases.
    normalized_name_to_code = {_normalize_team_name(rec["name"]): code for code, rec in team_records.items()}

    master_df = read_csv_safe(config.RAW_DATA_DIR / "_global" / "master_team_list.csv")
    for season in seasons:
        if id_to_code[season]:
            continue  # already resolved from that season's own teams.csv
        if master_df is None:
            continue
        for _, row in master_df[master_df["season"] == season].iterrows():
            team_id = int(row["team"])
            name = row["team_name"]
            code = name_to_code.get(name)
            if code is None:
                code = normalized_name_to_code.get(_normalize_team_name(name))
            if code is not None:
                id_to_code[season][team_id] = code
            else:
                unresolved.append((season, team_id, name))

    return team_records, name_to_code, season_name_to_code, id_to_code, unresolved


def _normalize_team_name(name: str) -> str:
    name = re.sub(r"\b(city|town|united|albion|athletic|hotspur)\b", "", name.lower())
    return re.sub(r"[^a-z]", "", name)


# ---------------------------------------------------------------------------
# Small per-table builders -> DataFrames (row counts in the hundreds/low
# thousands total across all seasons; plain loops are fast enough here)
# ---------------------------------------------------------------------------
def build_dim_season_df(seasons, source_ref, loaded_at):
    current = max(seasons)
    rows = [(s, int(s[:4]), s == current, SOURCE_NAME, source_ref, loaded_at) for s in seasons]
    return pd.DataFrame(rows, columns=["season_id", "start_year", "is_current", "source", "source_ref", "loaded_at"])


def build_dim_team_df(team_records, source_ref, loaded_at):
    rows = [(code, rec["name"], rec["short_name"], SOURCE_NAME, source_ref, loaded_at)
            for code, rec in team_records.items()]
    return pd.DataFrame(rows, columns=["team_code", "name", "short_name", "source", "source_ref", "loaded_at"])


def build_fact_team_season_df(seasons, id_to_code, source_ref, loaded_at):
    rows = []
    for season in seasons:
        df = read_csv_safe(season_dir(season) / "teams.csv")
        if df is None:
            continue
        for _, r in df.iterrows():
            code = id_to_code[season].get(int(r["id"]))
            if code is None:
                continue
            rows.append((
                season, code, int(r["id"]),
                nn(r.get("strength")), nn(r.get("strength_overall_home")), nn(r.get("strength_overall_away")),
                nn(r.get("strength_attack_home")), nn(r.get("strength_attack_away")),
                nn(r.get("strength_defence_home")), nn(r.get("strength_defence_away")),
                nn(r.get("played")), nn(r.get("win")), nn(r.get("draw")), nn(r.get("loss")),
                nn(r.get("points")), nn(r.get("position")),
                SOURCE_NAME, source_ref, loaded_at,
            ))
    cols = ["season_id", "team_code", "team_id_that_season", "strength", "strength_overall_home",
            "strength_overall_away", "strength_attack_home", "strength_attack_away", "strength_defence_home",
            "strength_defence_away", "played", "win", "draw", "loss", "points", "position",
            "source", "source_ref", "loaded_at"]
    return pd.DataFrame(rows, columns=cols)


def build_player_and_season_data(seasons, team_id_to_code, source_ref, loaded_at):
    """Returns dim_player df, fact_player_season df, player id->code map per
    season, and season_player_team lookup (fallback for old gw files that
    don't carry a per-gameweek team name)."""
    player_bio = {}                       # code -> bio dict (last write wins)
    player_id_to_code = {s: {} for s in seasons}
    season_player_team = {}               # (season, code) -> team_code
    fact_player_season_rows = []

    for season in seasons:
        df = read_csv_safe(season_dir(season) / "players_raw.csv")
        if df is None:
            continue
        for _, r in df.iterrows():
            code = int(r["code"])
            pid = int(r["id"])
            player_id_to_code[season][pid] = code
            player_bio[code] = {
                "first_name": nn(r.get("first_name")),
                "second_name": nn(r.get("second_name")),
                "web_name": nn(r.get("web_name")),
                "birth_date": nn(r.get("birth_date")),
                "region": nn(r.get("region")),
            }
            team_code = team_id_to_code.get(season, {}).get(int(r["team"])) if nn(r.get("team")) is not None else None
            season_player_team[(season, code)] = team_code
            fact_player_season_rows.append((
                season, code, pid, team_code,
                POSITION_MAP.get(int(r["element_type"])) if nn(r.get("element_type")) is not None else None,
                None,  # start_cost — backfilled later from gameweek data
                nn(r.get("now_cost")),
                nn(r.get("total_points")),
                nn(r.get("minutes")),
                nn(r.get("selected_by_percent")),
                nn(r.get("now_cost_rank")),
                nn(r.get("points_per_game")),
                nn(r.get("form")),
                SOURCE_NAME, source_ref, loaded_at,
            ))

    dim_player_rows = [
        (code, bio["first_name"], bio["second_name"], bio["web_name"], bio["birth_date"], bio["region"],
         SOURCE_NAME, source_ref, loaded_at)
        for code, bio in player_bio.items()
    ]
    dim_player_df = pd.DataFrame(dim_player_rows, columns=[
        "player_code", "first_name", "second_name", "web_name", "birth_date", "region",
        "source", "source_ref", "loaded_at"])
    fact_player_season_df = pd.DataFrame(fact_player_season_rows, columns=[
        "season_id", "player_code", "player_id_that_season", "team_code", "position",
        "start_cost", "end_cost", "total_points", "minutes", "selected_by_percent",
        "now_cost_rank", "points_per_game", "form", "source", "source_ref", "loaded_at"])
    return dim_player_df, fact_player_season_df, player_id_to_code, season_player_team


def build_fact_fixture_df(seasons, team_id_to_code, source_ref, loaded_at):
    rows = []
    for season in seasons:
        df = read_csv_safe(season_dir(season) / "fixtures.csv")
        if df is None:
            continue
        for _, r in df.iterrows():
            team_h_code = team_id_to_code.get(season, {}).get(int(r["team_h"])) if nn(r.get("team_h")) is not None else None
            team_a_code = team_id_to_code.get(season, {}).get(int(r["team_a"])) if nn(r.get("team_a")) is not None else None
            if team_h_code is None or team_a_code is None:
                continue  # unresolved team — skip rather than insert a broken FK
            rows.append((
                season, int(r["id"]), nn(r.get("event")),
                team_h_code, team_a_code,
                nn(r.get("team_h_score")), nn(r.get("team_a_score")),
                nn(r.get("kickoff_time")),
                nn(r.get("team_h_difficulty")), nn(r.get("team_a_difficulty")),
                bool(r["finished"]) if nn(r.get("finished")) is not None else None,
                SOURCE_NAME, source_ref, loaded_at,
            ))
    cols = ["season_id", "fixture_id", "gameweek", "team_h_code", "team_a_code", "team_h_score",
            "team_a_score", "kickoff_time", "team_h_difficulty", "team_a_difficulty", "finished",
            "source", "source_ref", "loaded_at"]
    return pd.DataFrame(rows, columns=cols)


# ---------------------------------------------------------------------------
# Player-gameweek facts — the ~254K-row table. Vectorized, no row loop.
# ---------------------------------------------------------------------------
FACT_PGW_SOURCE_COLS = [
    "minutes", "total_points", "goals_scored", "assists", "clean_sheets", "goals_conceded",
    "own_goals", "bonus", "bps", "influence", "creativity", "threat", "ict_index",
    "expected_goals", "expected_assists", "expected_goal_involvements", "expected_goals_conceded",
    "value", "selected", "transfers_in", "transfers_out", "transfers_balance",
    "yellow_cards", "red_cards", "saves", "penalties_saved", "penalties_missed",
]

FACT_PGW_COLS = (
    ["season_id", "gameweek", "player_code", "team_code", "opponent_team_code", "fixture_id",
     "was_home", "starts"] + FACT_PGW_SOURCE_COLS + ["source", "source_ref", "loaded_at"]
)


def build_fact_player_gameweek_df(seasons, player_id_to_code, team_id_to_code, name_to_code,
                                   season_name_to_code, season_player_team, source_ref, loaded_at):
    frames = []
    for season in seasons:
        df = read_csv_safe(season_dir(season) / "gws" / "merged_gw.csv")
        if df is None:
            continue

        gw_col = "GW" if "GW" in df.columns else "round"
        has_team_name = "team" in df.columns

        before = len(df)
        df = df.drop_duplicates(subset=["element", "fixture", gw_col]).copy()
        if len(df) != before:
            print(f"    [{season}] dropped {before - len(df)} exact-duplicate gw rows")

        pid_map = player_id_to_code.get(season, {})
        tid_map = team_id_to_code.get(season, {})

        out = pd.DataFrame(index=df.index)
        out["season_id"] = season
        out["gameweek"] = df[gw_col]
        out["player_code"] = df["element"].map(pid_map)
        out["fixture_id"] = df["fixture"]

        if has_team_name:
            # Prefer that season's own name->code mapping (a club's display
            # name can change between stints in the league, e.g. Ipswich /
            # Ipswich Town, even though its `code` stays the same).
            local_name_to_code = season_name_to_code.get(season) or name_to_code
            out["team_code"] = df["team"].map(local_name_to_code)
        else:
            season_team_lookup = {code: tc for (s, code), tc in season_player_team.items() if s == season}
            out["team_code"] = out["player_code"].map(season_team_lookup)

        out["opponent_team_code"] = df["opponent_team"].map(tid_map)

        out["was_home"] = df["was_home"].astype("boolean") if "was_home" in df.columns else pd.array([pd.NA] * len(df), dtype="boolean")
        out["starts"] = df["starts"].astype("boolean") if "starts" in df.columns else pd.array([pd.NA] * len(df), dtype="boolean")

        for col in FACT_PGW_SOURCE_COLS:
            out[col] = df[col] if col in df.columns else np.nan

        out["source"] = SOURCE_NAME
        out["source_ref"] = source_ref
        out["loaded_at"] = loaded_at

        # player_code/fixture_id are part of the primary key — can't be null
        out = out.dropna(subset=["player_code", "fixture_id", "gameweek"])
        out["player_code"] = out["player_code"].astype(int)
        out["fixture_id"] = out["fixture_id"].astype(int)
        out["gameweek"] = out["gameweek"].astype(int)

        frames.append(out[FACT_PGW_COLS])

    if not frames:
        return pd.DataFrame(columns=FACT_PGW_COLS)
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run():
    manifest = load_manifest()
    seasons = manifest["seasons"]
    source_ref = manifest["commit_sha"]
    loaded_at = datetime.now(timezone.utc)

    con = duckdb.connect(str(DB_PATH))
    run_id = con.execute(
        "INSERT INTO ingest_runs (source, source_ref, started_at, status) VALUES (?, ?, ?, 'running') RETURNING run_id",
        [SOURCE_NAME, source_ref, loaded_at],
    ).fetchone()[0]

    print(f"Run {run_id} | source_ref (commit) = {source_ref}")
    print(f"Seasons: {seasons}")

    print("\nBuilding team crosswalk...")
    team_records, name_to_code, season_name_to_code, team_id_to_code, unresolved_teams = build_team_crosswalk(seasons)
    print(f"  resolved {len(team_records)} distinct teams")
    if unresolved_teams:
        print(f"  WARNING: {len(unresolved_teams)} (season, id, name) team refs unresolved:")
        for season, tid, name in unresolved_teams:
            print(f"    - {season}: id={tid} name={name!r}")

    print("\nBuilding player bios and season snapshots...")
    dim_player_df, fact_player_season_df, player_id_to_code, season_player_team = build_player_and_season_data(
        seasons, team_id_to_code, source_ref, loaded_at
    )
    print(f"  {len(dim_player_df)} distinct players, {len(fact_player_season_df)} player-season rows")

    print("\nBuilding fixtures...")
    fact_fixture_df = build_fact_fixture_df(seasons, team_id_to_code, source_ref, loaded_at)
    print(f"  {len(fact_fixture_df)} fixture rows")

    print("\nBuilding player-gameweek facts (vectorized)...")
    fact_pgw_df = build_fact_player_gameweek_df(
        seasons, player_id_to_code, team_id_to_code, name_to_code, season_name_to_code,
        season_player_team, source_ref, loaded_at
    )
    print(f"  {len(fact_pgw_df)} player-gameweek rows")

    dim_season_df = build_dim_season_df(seasons, source_ref, loaded_at)
    dim_team_df = build_dim_team_df(team_records, source_ref, loaded_at)
    fact_team_season_df = build_fact_team_season_df(seasons, team_id_to_code, source_ref, loaded_at)

    print("\nLoading into DuckDB (upsert — nothing is deleted)...")
    con.execute("BEGIN")
    try:
        def upsert_df(table, df):
            if len(df) == 0:
                return
            pk_cols = con.execute(
                "SELECT constraint_column_names FROM duckdb_constraints() "
                "WHERE table_name = ? AND constraint_type = 'PRIMARY KEY'",
                [table],
            ).fetchone()[0]
            update_cols = [c for c in df.columns if c not in pk_cols]
            set_clause = ", ".join(f"{c} = excluded.{c}" for c in update_cols)
            con.execute(
                f"INSERT INTO {table} BY NAME SELECT * FROM df "
                f"ON CONFLICT ({', '.join(pk_cols)}) DO UPDATE SET {set_clause}"
            )

        # Order matters: dims before facts, so a fact row's FK always finds
        # its dimension row already present (or upserted this same pass).
        upsert_df("dim_season", dim_season_df)
        upsert_df("dim_team", dim_team_df)
        upsert_df("dim_player", dim_player_df)
        upsert_df("fact_team_season", fact_team_season_df)
        upsert_df("fact_player_season", fact_player_season_df)
        upsert_df("fact_fixture", fact_fixture_df)
        upsert_df("fact_player_gameweek", fact_pgw_df)

        # Backfill start_cost from the earliest gameweek's price per player-season.
        con.execute("""
            UPDATE fact_player_season fs
            SET start_cost = sub.start_cost
            FROM (
                SELECT season_id, player_code, arg_min(value, gameweek) AS start_cost
                FROM fact_player_gameweek
                GROUP BY season_id, player_code
            ) sub
            WHERE fs.season_id = sub.season_id AND fs.player_code = sub.player_code
        """)

        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        con.execute(
            "UPDATE ingest_runs SET status='failed', finished_at=? WHERE run_id=?",
            [datetime.now(timezone.utc), run_id],
        )
        raise

    total_rows = (len(dim_season_df) + len(dim_team_df) + len(dim_player_df)
                  + len(fact_team_season_df) + len(fact_player_season_df)
                  + len(fact_fixture_df) + len(fact_pgw_df))
    notes = f"unresolved_team_refs={len(unresolved_teams)}"
    con.execute(
        "UPDATE ingest_runs SET status='success', finished_at=?, rows_affected=?, notes=? WHERE run_id=?",
        [datetime.now(timezone.utc), total_rows, notes, run_id],
    )
    con.close()
    print(f"\nDone. {total_rows} total rows loaded across all tables.")


if __name__ == "__main__":
    run()
