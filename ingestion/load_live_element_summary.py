"""Load a live_element_summary.py backfill (stage C) into DuckDB.

Fills the weekly market columns that event/<gw>/live can't supply — `value`,
`selected`, `transfers_in/out/balance` — on fact_player_gameweek rows that
ingestion A already created (docs/live_api_connector_plan.md, item C2).

Design choices worth knowing:
- Rows are matched on the full PK (gameweek, player_code, fixture_id) with an
  inner join against what is already stored. upsert_df is INSERT ... ON
  CONFLICT, so without this a history row for a gameweek A hasn't loaded yet
  (the in-progress one) would insert a mostly-NULL row. Unmatched rows on
  either side are reported, never inserted.
- Stats (points, minutes, defensive stats) are NOT overwritten: A already
  filled them. They are compared as a cross-check that both endpoints agree.
- Only the market columns (plus lineage) are in the upsert frame, so nothing
  else on the row is touched.

Run from ingestion/ with the venv active; the backend must be stopped (DuckDB
lock). Optional argument: path to a DB file, to test on a copy first.
    python load_live_element_summary.py [db_path]
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

import config
from common import upsert_df
from live_element_summary import latest_pull_dir

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "db" / "epl.duckdb"
SOURCE_NAME = "live_api"

MARKET_COLS = ["value", "selected", "transfers_in", "transfers_out", "transfers_balance"]
# Compared against stored values, never written.
CHECK_COLS = ["total_points", "minutes", "tackles", "recoveries",
              "clearances_blocks_interceptions", "defensive_contribution"]
KEY = ["gameweek", "player_code", "fixture_id"]


def read_history(pull, elements):
    """All players' per-fixture history rows as one frame, keyed by player_code."""
    rows = []
    for f in sorted((pull / "element_summary").glob("[0-9]*.json")):
        rows.extend(json.loads(f.read_text())["history"])
    h = pd.DataFrame(rows)
    h["player_code"] = h["element"].map(dict(zip((e["id"] for e in elements), (e["code"] for e in elements))))
    if h["player_code"].isna().any():
        raise RuntimeError("element-summary has players missing from bootstrap-static")
    h = h.rename(columns={"round": "gameweek", "fixture": "fixture_id"})
    if h.duplicated(KEY).any():
        raise RuntimeError("duplicate (gameweek, player_code, fixture_id) in element-summary history")
    return h


def run(db_path=DEFAULT_DB_PATH):
    pull = latest_pull_dir()
    if not (pull / "element_summary").is_dir():
        raise FileNotFoundError(f"{pull}/element_summary missing; run live_element_summary.py first")
    manifest = json.loads((pull / "element_summary" / "manifest.json").read_text())
    season_id = json.loads((pull / "manifest.json").read_text())["season_id"]
    source_ref = manifest["fetched_at"]
    loaded_at = datetime.now(timezone.utc)
    history = read_history(pull, json.loads((pull / "bootstrap-static.json").read_text())["elements"])

    con = duckdb.connect(str(db_path))
    run_id = con.execute(
        "INSERT INTO ingest_runs (source, source_ref, started_at, status) VALUES (?, ?, ?, 'running') RETURNING run_id",
        [SOURCE_NAME, source_ref, loaded_at]).fetchone()[0]
    print(f"Run {run_id} | pull {pull.name} | season {season_id} | db {db_path}")

    stored = con.execute(
        f"SELECT {', '.join(KEY + MARKET_COLS + CHECK_COLS)} FROM fact_player_gameweek WHERE season_id = ?",
        [season_id]).df()
    m = stored.merge(history, on=KEY, how="outer", suffixes=("_db", "_api"), indicator=True)

    only_api = m[m["_merge"] == "right_only"]
    only_db = m[m["_merge"] == "left_only"]
    both = m[m["_merge"] == "both"]
    print(f"  history rows {len(history)}, stored rows {len(stored)}, matched {len(both)}")
    print(f"  history rows with no stored row (not loaded): {len(only_api)}"
          f" (gameweeks {sorted(only_api['gameweek'].unique().tolist())})")
    print(f"  stored rows with no history row: {len(only_db)}")

    # Cross-checks on matched rows: stats should agree; GW1 market values came
    # from the GitHub source, so they should agree too.
    for c in CHECK_COLS:
        print(f"  check {c}: {int((both[c + '_db'] != both[c + '_api']).sum())} mismatches")
    gw1 = both[both["gameweek"] == 1]
    for c in MARKET_COLS:
        print(f"  check GW1 {c} vs GitHub-sourced: {int((gw1[c + '_db'] != gw1[c + '_api']).sum())} mismatches")

    out = history.merge(stored[KEY], on=KEY, how="inner")[KEY + MARKET_COLS]
    out = out.assign(season_id=season_id, source=SOURCE_NAME, source_ref=source_ref, loaded_at=loaded_at)
    out = out.astype({"value": "int64", "selected": "int64", "transfers_in": "int64",
                      "transfers_out": "int64", "transfers_balance": "int64"})

    con.execute("BEGIN")
    try:
        upsert_df(con, "fact_player_gameweek", out)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        con.execute("UPDATE ingest_runs SET status='failed', finished_at=? WHERE run_id=?",
                    [datetime.now(timezone.utc), run_id])
        raise

    gws = sorted(out["gameweek"].unique().tolist())
    notes = (f"stage=element_summary; market columns on {len(out)} rows, GW{gws[0]}-{gws[-1]}; "
             f"history_without_stored_row={len(only_api)}; stored_without_history={len(only_db)}")
    con.execute("UPDATE ingest_runs SET status='success', finished_at=?, rows_affected=?, notes=? WHERE run_id=?",
                [datetime.now(timezone.utc), len(out), notes, run_id])
    con.close()
    print(f"Done. {len(out)} rows updated.")


if __name__ == "__main__":
    run(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB_PATH)
