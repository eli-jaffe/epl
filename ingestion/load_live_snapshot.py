"""Load a live_snapshot.py pull (stage B) into fact_player_snapshot.

Append-only by design: every snapshot has its own `snapshot_at` (the fetch
time), which is part of the primary key, so each run adds ~one row per player
and earlier snapshots are never touched. Loading the same pull twice is still
idempotent, because the second load upserts onto identical keys.

Players not yet in dim_player (a new signing the API lists before the stats
refresh has seen them) are skipped and counted, not failed, so an unattended
daily run still captures everyone else; running load_live.py first adds them.

Run from ingestion/ with the venv active; the backend must be stopped (DuckDB
lock). Optional argument: path to a DB file, to test on a copy first.
    python load_live_snapshot.py [db_path]
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

import config
from common import upsert_df

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "db" / "epl.duckdb"
SOURCE_NAME = "live_api"


def latest_snapshot_dir() -> Path:
    """Newest complete snapshot; .partial directories are failed extracts."""
    snaps = sorted(d for d in config.LIVE_SNAPSHOT_RAW_DIR.iterdir()
                   if d.is_dir() and not d.name.endswith(".partial"))
    if not snaps:
        raise FileNotFoundError(f"no complete snapshot in {config.LIVE_SNAPSHOT_RAW_DIR}; run live_snapshot.py first")
    return snaps[-1]


def build_snapshot_df(bootstrap, season_id, snapshot_at, source_ref, loaded_at):
    e = pd.DataFrame(bootstrap["elements"])
    current = [ev["id"] for ev in bootstrap["events"] if ev["is_current"]]
    return pd.DataFrame({
        "season_id": season_id,
        "player_code": e["code"],
        "snapshot_at": snapshot_at,
        # The API's current event. Right after a gameweek finishes this still
        # names that gameweek even though prices now apply to the next one, so
        # the planned view matches snapshots to gameweeks by time instead.
        "gameweek": current[0] if current else None,
        "now_cost": e["now_cost"],
        "selected_by_percent": pd.to_numeric(e["selected_by_percent"]),
        "total_players": bootstrap["total_players"],
        "transfers_in_event": e["transfers_in_event"],
        "transfers_out_event": e["transfers_out_event"],
        "cost_change_event": e["cost_change_event"],
        "source": SOURCE_NAME, "source_ref": source_ref, "loaded_at": loaded_at,
    })


def run(db_path=DEFAULT_DB_PATH):
    snap = latest_snapshot_dir()
    manifest = json.loads((snap / "manifest.json").read_text())
    bootstrap = json.loads((snap / "bootstrap-static.json").read_text())
    season_id = manifest["season_id"]
    source_ref = manifest["fetched_at"]
    # naive UTC, same convention as fact_fixture.kickoff_time
    snapshot_at = datetime.fromisoformat(source_ref).astimezone(timezone.utc).replace(tzinfo=None)
    loaded_at = datetime.now(timezone.utc)

    con = duckdb.connect(str(db_path))
    run_id = con.execute(
        "INSERT INTO ingest_runs (source, source_ref, started_at, status) VALUES (?, ?, ?, 'running') RETURNING run_id",
        [SOURCE_NAME, source_ref, loaded_at]).fetchone()[0]
    print(f"Run {run_id} | snapshot {snap.name} | season {season_id} | db {db_path}")

    try:
        if not con.execute("SELECT count(*) FROM dim_season WHERE season_id = ?", [season_id]).fetchone()[0]:
            raise RuntimeError(f"season {season_id} not in dim_season; run live_api.py and load_live.py first")
        df = build_snapshot_df(bootstrap, season_id, snapshot_at, source_ref, loaded_at)
        known = set(con.execute("SELECT player_code FROM dim_player").df()["player_code"])
        skipped = df[~df["player_code"].isin(known)]
        df = df[df["player_code"].isin(known)]
        print(f"  {len(df)} players, {len(skipped)} skipped (not in dim_player); "
              f"gameweek {df['gameweek'].iloc[0]}, snapshot_at {snapshot_at}")

        con.execute("BEGIN")
        try:
            upsert_df(con, "fact_player_snapshot", df)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    except Exception:
        con.execute("UPDATE ingest_runs SET status='failed', finished_at=? WHERE run_id=?",
                    [datetime.now(timezone.utc), run_id])
        raise

    notes = (f"stage=snapshot; players={len(df)}; skipped_not_in_dim_player={len(skipped)}; "
             f"gameweek={df['gameweek'].iloc[0]}")
    con.execute("UPDATE ingest_runs SET status='success', finished_at=?, rows_affected=?, notes=? WHERE run_id=?",
                [datetime.now(timezone.utc), len(df), notes, run_id])
    con.close()
    print(f"Done. {len(df)} rows upserted.")


if __name__ == "__main__":
    run(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB_PATH)
