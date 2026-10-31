"""Load a live_api.py pull (stage A: stats refresh) into DuckDB.

Reads the newest complete directory under config.LIVE_RAW_DATA_DIR and
upserts into the same tables the historical loader fills. Built in pieces,
dims first: this file currently loads dim_season, dim_team and dim_player
(plan items A2, A3, R1); the season/fixture/gameweek facts are added next
(docs/live_api_connector_plan.md).

Like the historical loader, everything is an idempotent upsert keyed on FPL's
stable `code`, never the season-scoped `id`.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

import config
from common import upsert_df

DB_PATH = Path(__file__).resolve().parent.parent / "db" / "epl.duckdb"
SOURCE_NAME = "live_api"

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

    con.execute("BEGIN")
    try:
        # Dims before facts so later fact rows always find their parents.
        upsert_df(con, "dim_season", dim_season_df)
        con.execute("UPDATE dim_season SET is_current = (season_id = ?)", [season_id])
        upsert_df(con, "dim_team", dim_team_df)
        upsert_df(con, "dim_player", dim_player_df)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        con.execute("UPDATE ingest_runs SET status='failed', finished_at=? WHERE run_id=?",
                    [datetime.now(timezone.utc), run_id])
        raise

    total_rows = len(dim_season_df) + len(dim_team_df) + len(dim_player_df)
    notes = f"dims only; new_players={n_new}; changed_players={n_changed}"
    con.execute("UPDATE ingest_runs SET status='success', finished_at=?, rows_affected=?, notes=? WHERE run_id=?",
                [datetime.now(timezone.utc), total_rows, notes, run_id])
    con.close()
    print(f"Done. {total_rows} dim rows upserted.")


if __name__ == "__main__":
    run()
