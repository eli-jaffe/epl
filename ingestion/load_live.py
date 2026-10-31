"""Load a live_api.py pull (stage A: stats refresh) into DuckDB.

Reads the newest complete directory under config.LIVE_RAW_DATA_DIR and
upserts into the same tables the historical loader fills: dim_season,
dim_team, dim_player, fact_team_season, fact_player_season, fact_fixture and
fact_player_gameweek (docs/live_api_connector_plan.md, items A1-A7, R1, R2).

Like the historical loader, everything is an idempotent upsert keyed on FPL's
stable `code`, never the season-scoped `id`. Columns this source can't supply
(weekly price/ownership/transfers, standings) are left out of the frames, so
the upsert never overwrites existing values with NULL.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

import config
from common import upsert_df

DB_PATH = Path(__file__).resolve().parent.parent / "db" / "epl.duckdb"
SOURCE_NAME = "live_api"

POSITION_MAP = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD", 5: "AM"}

# event/<gw>/live `stats` keys that map onto fact_player_gameweek columns.
GW_INT_STATS = ["minutes", "total_points", "goals_scored", "assists", "clean_sheets", "goals_conceded",
                "own_goals", "bonus", "bps", "yellow_cards", "red_cards", "saves", "penalties_saved",
                "penalties_missed", "tackles", "recoveries", "clearances_blocks_interceptions",
                "defensive_contribution"]
# The API sends these as strings ("18.6").
GW_FLOAT_STATS = ["influence", "creativity", "threat", "ict_index", "expected_goals", "expected_assists",
                  "expected_goal_involvements", "expected_goals_conceded"]

PLAYER_BIO_COLS = ["first_name", "second_name", "web_name", "birth_date", "region"]


def latest_pull_dir() -> Path:
    """Newest complete pull. Directories ending in .partial are failed or
    in-flight extracts (see live_api.py) and are skipped."""
    pulls = sorted(d for d in config.LIVE_RAW_DATA_DIR.iterdir()
                   if d.is_dir() and not d.name.endswith(".partial"))
    if not pulls:
        raise FileNotFoundError(f"no complete live_api pull in {config.LIVE_RAW_DATA_DIR}; run live_api.py first")
    return pulls[-1]


def build_dim_season_df(season_id, source_ref, loaded_at):
    return pd.DataFrame([(season_id, int(season_id[:4]), True, SOURCE_NAME, source_ref, loaded_at)],
                        columns=["season_id", "start_year", "is_current", "source", "source_ref", "loaded_at"])


def build_dim_team_df(teams, source_ref, loaded_at):
    df = pd.DataFrame(teams)[["code", "name", "short_name"]].rename(columns={"code": "team_code"})
    return df.assign(source=SOURCE_NAME, source_ref=source_ref, loaded_at=loaded_at)


def _as_date(col):
    """-> python dates with None (not NaT) for missing, so == comparisons work."""
    dates = pd.to_datetime(col, errors="coerce").dt.date.astype(object)
    return dates.where(pd.to_datetime(col, errors="coerce").notna(), None)


def build_dim_player_df(elements, existing, source_ref, loaded_at):
    """Reconcile live bios with dim_player (item R1). Live is fresher, so it
    wins for names/region (e.g. 'Angulo' -> 'N.Angulo'), but a missing live
    value never erases a known one — the API leaves birth_date empty for
    ~2% of players, and generic upsert_df would overwrite it with NULL.

    Returns (df to upsert, number of new players, number of changed players).
    """
    live = pd.DataFrame(elements)[["code"] + PLAYER_BIO_COLS].rename(columns={"code": "player_code"})
    live["birth_date"] = _as_date(live["birth_date"])
    live = live.astype(object).where(live.notna(), None)

    # DuckDB hands birth_date back as Timestamp; match the live side's date.
    existing = existing.assign(birth_date=_as_date(existing["birth_date"]))
    old = existing.set_index("player_code")
    merged = live.set_index("player_code")
    is_new = ~merged.index.isin(old.index)

    prior = old.reindex(merged.index)[PLAYER_BIO_COLS].astype(object)
    prior = prior.where(prior.notna(), None)
    for col in PLAYER_BIO_COLS:
        merged[col] = merged[col].where(merged[col].notna(), prior[col])

    # pandas treats None like NaN, so None != None is True; count two nulls
    # as equal explicitly (200 vs 200.0 already compare equal).
    differs = pd.concat(
        [~((merged[c] == prior[c]) | (merged[c].isna() & prior[c].isna())) for c in PLAYER_BIO_COLS],
        axis=1).any(axis=1)
    changed = (~is_new) & differs

    out = merged.reset_index().assign(source=SOURCE_NAME, source_ref=source_ref, loaded_at=loaded_at)
    return out, int(is_new.sum()), int(changed.sum())


def build_fact_team_season_df(teams, season_id, source_ref, loaded_at):
    """Strength ratings only. The API's played/win/draw/loss/points/position
    are all zero or stale here, so they are left out rather than overwriting
    the existing columns (standings from fixtures is a plan follow-up)."""
    t = pd.DataFrame(teams)
    cols = ["strength_overall_home", "strength_overall_away", "strength_attack_home",
            "strength_attack_away", "strength_defence_home", "strength_defence_away"]
    df = t[["code", "id"] + cols].rename(columns={"code": "team_code", "id": "team_id_that_season"})
    return df.assign(season_id=season_id, source=SOURCE_NAME, source_ref=source_ref, loaded_at=loaded_at)


def build_fact_player_season_df(elements, team_id_to_code, season_id, gw_totals, source_ref, loaded_at):
    """gw_totals: per-player total_points/minutes summed over the gameweeks
    being loaded. The API's own season totals include the in-progress
    gameweek, which would make this table disagree with fact_player_gameweek."""
    e = pd.DataFrame(elements)
    pts = e["code"].map(gw_totals["total_points"]).fillna(0).astype(int)
    mins = e["code"].map(gw_totals["minutes"]).fillna(0).astype(int)
    return pd.DataFrame({
        "season_id": season_id,
        "player_code": e["code"],
        "player_id_that_season": e["id"],
        "team_code": e["team"].map(team_id_to_code),   # current club; a mid-season mover keeps only their latest here
        "position": e["element_type"].map(POSITION_MAP),
        # start price = today's price minus the change since the season began;
        # matched the warehouse's GW1-derived start_cost on 610/610 players.
        "start_cost": e["now_cost"] - e["cost_change_start"],
        "end_cost": e["now_cost"],                    # historical convention: now_cost
        "total_points": pts,
        "minutes": mins,
        "selected_by_percent": pd.to_numeric(e["selected_by_percent"]),
        "now_cost_rank": e["now_cost_rank"],
        "points_per_game": pd.to_numeric(e["points_per_game"]),
        "form": pd.to_numeric(e["form"]),
        "source": SOURCE_NAME, "source_ref": source_ref, "loaded_at": loaded_at,
    })


def build_fact_fixture_df(fixtures, team_id_to_code, season_id, source_ref, loaded_at):
    f = pd.DataFrame(fixtures)
    return pd.DataFrame({
        "season_id": season_id,
        "fixture_id": f["id"],
        "gameweek": f["event"].astype("Int64"),
        "team_h_code": f["team_h"].map(team_id_to_code),
        "team_a_code": f["team_a"].map(team_id_to_code),
        "team_h_score": f["team_h_score"].astype("Int64"),
        "team_a_score": f["team_a_score"].astype("Int64"),
        # naive UTC, same as the historical rows
        "kickoff_time": pd.to_datetime(f["kickoff_time"], utc=True).dt.tz_localize(None),
        "team_h_difficulty": f["team_h_difficulty"],
        "team_a_difficulty": f["team_a_difficulty"],
        "finished": f["finished"],
        "source": SOURCE_NAME, "source_ref": source_ref, "loaded_at": loaded_at,
    })


def build_fact_player_gameweek_df(pull, gameweeks, elements, fixture_df, team_id_to_code,
                                  existing_gw, season_id, source_ref, loaded_at):
    """One row per explain entry (= per fixture) from each event/<gw>/live file.

    event/live `stats` is the player's total for the gameweek, so it only
    equals a per-fixture row when the player had a single fixture. A double
    gameweek would need element-summary for the non-scoring stats (plan item
    C2); until that exists we stop rather than load aggregates onto each row.

    Returns (df, number of rows whose club could not be resolved).
    """
    records = []
    for gw in gameweeks:
        for el in json.loads((pull / "event_live" / f"{gw}.json").read_text())["elements"]:
            for x in el["explain"]:
                records.append({"gameweek": gw, "element": el["id"], "fixture_id": x["fixture"],
                                "n_fixtures": len(el["explain"]), **el["stats"]})
    live = pd.DataFrame(records)

    multi = live[live["n_fixtures"] > 1]
    if len(multi):
        raise RuntimeError(
            f"{multi['element'].nunique()} player(s) have more than one fixture in a gameweek "
            f"(e.g. element {int(multi.iloc[0]['element'])} in GW{int(multi.iloc[0]['gameweek'])}); "
            "double gameweeks need the element-summary path (plan item C2) before they can be loaded")

    el = pd.DataFrame(elements)
    live["player_code"] = live["element"].map(dict(zip(el["id"], el["code"])))
    live["cur_team"] = live["element"].map(dict(zip(el["id"], el["team"]))).map(team_id_to_code)
    live = live.merge(fixture_df[["fixture_id", "team_h_code", "team_a_code"]], on="fixture_id", how="left")

    # Which club was the player playing for? The API only gives the *current*
    # club, which is wrong for players who moved mid-season. Prefer, in order:
    # the current club if it is in the fixture, the club already stored for
    # this exact row, then the player's latest stored club if it is in the fixture.
    stored = existing_gw[["gameweek", "player_code", "fixture_id", "team_code"]].rename(columns={"team_code": "stored_team"})
    live = live.merge(stored, on=["gameweek", "player_code", "fixture_id"], how="left")
    last_stored = (existing_gw.sort_values("gameweek").groupby("player_code")["team_code"].last()
                   .rename("last_team"))
    live = live.join(last_stored, on="player_code")

    def in_fixture(col):
        return live[col].notna() & ((live[col] == live["team_h_code"]) | (live[col] == live["team_a_code"]))

    team = pd.Series(pd.NA, index=live.index, dtype="Float64")
    for col in ["last_team", "stored_team", "cur_team"]:   # later assignments win
        team = team.where(~in_fixture(col), live[col].astype("Float64"))
    unresolved = int(team.isna().sum())

    out = pd.DataFrame({
        "season_id": season_id, "gameweek": live["gameweek"], "player_code": live["player_code"],
        "team_code": team.astype("Int64"),
        "opponent_team_code": pd.Series(np.where(team == live["team_h_code"], live["team_a_code"],
                                        np.where(team == live["team_a_code"], live["team_h_code"], np.nan)),
                                        index=live.index).astype("Int64"),
        "fixture_id": live["fixture_id"],
        "was_home": pd.Series(np.where(team.isna(), None, team == live["team_h_code"]), index=live.index),
        "starts": live["starts"].astype(bool),
    })
    for c in GW_INT_STATS:
        out[c] = live[c]
    for c in GW_FLOAT_STATS:
        out[c] = pd.to_numeric(live[c])
    # selected/value/transfers_* are not in this endpoint: omitted, never nulled.
    out = out.assign(source=SOURCE_NAME, source_ref=source_ref, loaded_at=loaded_at)
    return out, unresolved


def run():
    pull = latest_pull_dir()
    manifest = json.loads((pull / "manifest.json").read_text())
    bootstrap = json.loads((pull / "bootstrap-static.json").read_text())
    season_id = manifest["season_id"]
    source_ref = manifest["fetched_at"]
    loaded_at = datetime.now(timezone.utc)

    con = duckdb.connect(str(DB_PATH))
    run_id = con.execute(
        "INSERT INTO ingest_runs (source, source_ref, started_at, status) VALUES (?, ?, ?, 'running') RETURNING run_id",
        [SOURCE_NAME, source_ref, loaded_at],
    ).fetchone()[0]
    print(f"Run {run_id} | pull {pull.name} | season {season_id}")

    existing = con.execute("SELECT player_code, " + ", ".join(PLAYER_BIO_COLS) + " FROM dim_player").df()
    dim_season_df = build_dim_season_df(season_id, source_ref, loaded_at)
    dim_team_df = build_dim_team_df(bootstrap["teams"], source_ref, loaded_at)
    dim_player_df, n_new, n_changed = build_dim_player_df(bootstrap["elements"], existing, source_ref, loaded_at)
    print(f"  {len(dim_team_df)} teams, {len(dim_player_df)} players ({n_new} new, {n_changed} with changed bio)")

    team_id_to_code = {t["id"]: t["code"] for t in bootstrap["teams"]}
    # Only finished gameweeks are loaded: stats (bonus/BPS especially) still
    # move while a gameweek is in progress. The raw pull keeps everything; the
    # current gameweek lands on the first run after it closes.
    finished = {e["id"] for e in bootstrap["events"] if e["finished"]}
    gameweeks = [gw for gw in manifest["gameweeks"] if gw in finished]
    skipped = [gw for gw in manifest["gameweeks"] if gw not in finished]
    print(f"  loading GW{gameweeks[0]}-{gameweeks[-1]}; skipping unfinished: {skipped or 'none'}"
          if gameweeks else f"  no finished gameweeks yet; skipping {skipped}")

    fact_team_season_df = build_fact_team_season_df(bootstrap["teams"], season_id, source_ref, loaded_at)
    fact_fixture_df = build_fact_fixture_df(json.loads((pull / "fixtures.json").read_text()), team_id_to_code,
                                            season_id, source_ref, loaded_at)
    existing_gw = con.execute(
        "SELECT gameweek, player_code, fixture_id, team_code FROM fact_player_gameweek WHERE season_id = ?",
        [season_id]).df()
    if gameweeks:
        fact_pgw_df, n_unresolved = build_fact_player_gameweek_df(
            pull, gameweeks, bootstrap["elements"], fact_fixture_df, team_id_to_code,
            existing_gw, season_id, source_ref, loaded_at)
        gw_totals = fact_pgw_df.groupby("player_code")[["total_points", "minutes"]].sum()
    else:
        fact_pgw_df, n_unresolved = pd.DataFrame(), 0
        gw_totals = pd.DataFrame(columns=["total_points", "minutes"])
    fact_player_season_df = build_fact_player_season_df(bootstrap["elements"], team_id_to_code, season_id,
                                                        gw_totals, source_ref, loaded_at)
    print(f"  {len(fact_player_season_df)} player-seasons, {len(fact_fixture_df)} fixtures, "
          f"{len(fact_pgw_df)} player-gameweek rows ({n_unresolved} with unresolved club)")

    con.execute("BEGIN")
    try:
        # Dims before facts so later fact rows always find their parents.
        upsert_df(con, "dim_season", dim_season_df)
        con.execute("UPDATE dim_season SET is_current = (season_id = ?)", [season_id])
        upsert_df(con, "dim_team", dim_team_df)
        upsert_df(con, "dim_player", dim_player_df)
        upsert_df(con, "fact_team_season", fact_team_season_df)
        upsert_df(con, "fact_player_season", fact_player_season_df)
        upsert_df(con, "fact_fixture", fact_fixture_df)
        upsert_df(con, "fact_player_gameweek", fact_pgw_df)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        con.execute("UPDATE ingest_runs SET status='failed', finished_at=? WHERE run_id=?",
                    [datetime.now(timezone.utc), run_id])
        raise

    total_rows = (len(dim_season_df) + len(dim_team_df) + len(dim_player_df) + len(fact_team_season_df)
                  + len(fact_player_season_df) + len(fact_fixture_df) + len(fact_pgw_df))
    notes = (f"new_players={n_new}; changed_players={n_changed}; gameweeks={f'{gameweeks[0]}-{gameweeks[-1]}' if gameweeks else 'none'}; skipped_unfinished={skipped}; unresolved_club_rows={n_unresolved}")
    con.execute("UPDATE ingest_runs SET status='success', finished_at=?, rows_affected=?, notes=? WHERE run_id=?",
                [datetime.now(timezone.utc), total_rows, notes, run_id])
    con.close()
    print(f"Done. {total_rows} total rows upserted.")


if __name__ == "__main__":
    run()
