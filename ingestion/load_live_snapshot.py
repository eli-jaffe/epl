"""Load live_snapshot.py pulls (stage B) into fact_player_snapshot.

Append-only by design: every snapshot has its own `snapshot_at` (the fetch
time), which is part of the primary key, so each snapshot adds ~one row per
player and earlier snapshots are never touched.

Catch-up: this loads *every* snapshot directory whose (season, snapshot_at) is
not already in the table, oldest first, not just the newest. A snapshot can
never be re-fetched, so the raw file is the source of truth and the DB just
needs to catch up whenever it next gets the chance — a scheduled fetch that
ran while the DB was locked or the laptop was off loses nothing. Loading is
idempotent (already-loaded snapshots are skipped; upserts converge anyway).
A directory holds bootstrap-static.json or, when fetched by the GitHub Actions
workflow, bootstrap-static.json.gz.

Players not yet in dim_player (a new signing the API lists before the stats
refresh has seen them) are skipped and counted, not failed, so an unattended
run still captures everyone else; running load_live.py first adds them.

Run from ingestion/ with the venv active. Optional arguments: a DB path (to
test on a copy) and a snapshots directory.
    python load_live_snapshot.py [db_path] [snapshots_dir]
"""
import gzip
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

import config
from common import upsert_df

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "db" / "epl.duckdb"
SOURCE_NAME = "live_api"

# Same patience as agent/observability_sync.py: the backend may briefly hold
# DuckDB's single-writer lock.
MAX_CONNECT_ATTEMPTS = 10
RETRY_DELAY_SECONDS = 3.0


def connect_with_retry(db_path):
    for attempt in range(1, MAX_CONNECT_ATTEMPTS + 1):
        try:
            return duckdb.connect(str(db_path))
        except duckdb.IOException:
            if attempt == MAX_CONNECT_ATTEMPTS:
                raise RuntimeError(
                    f"could not get the DuckDB write lock after {MAX_CONNECT_ATTEMPTS} attempts "
                    "(is the backend running?); the raw snapshots are safe and the next run catches up")
            time.sleep(RETRY_DELAY_SECONDS)


def snapshot_dirs(root: Path):
    """Complete snapshot directories, oldest first; .partial ones are failed extracts."""
    return sorted(d for d in root.iterdir() if d.is_dir() and not d.name.endswith(".partial"))


def read_bootstrap(d: Path):
    gz = d / "bootstrap-static.json.gz"
    if gz.exists():
        with gzip.open(gz, "rt") as f:
            return json.load(f)
    return json.loads((d / "bootstrap-static.json").read_text())


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


def run(db_path=DEFAULT_DB_PATH, snapshots_root=None):
    root = Path(snapshots_root) if snapshots_root else config.LIVE_SNAPSHOT_RAW_DIR
    dirs = snapshot_dirs(root)
    if not dirs:
        raise FileNotFoundError(f"no complete snapshot in {root}; run live_snapshot.py first")

    con = connect_with_retry(db_path)
    loaded = set(con.execute("SELECT DISTINCT season_id, snapshot_at FROM fact_player_snapshot").fetchall())
    todo = []
    for d in dirs:
        m = json.loads((d / "manifest.json").read_text())
        # naive UTC, same convention as fact_fixture.kickoff_time
        snapshot_at = datetime.fromisoformat(m["fetched_at"]).astimezone(timezone.utc).replace(tzinfo=None)
        if (m["season_id"], snapshot_at) not in loaded:
            todo.append((d, m, snapshot_at))
    print(f"{len(dirs)} snapshot dir(s), {len(todo)} not yet in the DB")
    if not todo:
        con.close()
        return

    loaded_at = datetime.now(timezone.utc)
    source_ref = todo[-1][1]["fetched_at"]
    run_id = con.execute(
        "INSERT INTO ingest_runs (source, source_ref, started_at, status) VALUES (?, ?, ?, 'running') RETURNING run_id",
        [SOURCE_NAME, source_ref, loaded_at]).fetchone()[0]
    print(f"Run {run_id} | db {db_path}")

    total_rows = total_skipped = 0
    try:
        known = set(con.execute("SELECT player_code FROM dim_player").df()["player_code"])
        con.execute("BEGIN")
        try:
            for d, m, snapshot_at in todo:
                if not con.execute("SELECT count(*) FROM dim_season WHERE season_id = ?", [m["season_id"]]).fetchone()[0]:
                    raise RuntimeError(f"season {m['season_id']} not in dim_season; "
                                       "run live_api.py and load_live.py first")
                df = build_snapshot_df(read_bootstrap(d), m["season_id"], snapshot_at, m["fetched_at"], loaded_at)
                skipped = int((~df["player_code"].isin(known)).sum())
                df = df[df["player_code"].isin(known)]
                upsert_df(con, "fact_player_snapshot", df)
                total_rows += len(df)
                total_skipped += skipped
                print(f"  {d.name}: {len(df)} players, {skipped} skipped (not in dim_player), "
                      f"gameweek {df['gameweek'].iloc[0]}")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    except Exception:
        con.execute("UPDATE ingest_runs SET status='failed', finished_at=? WHERE run_id=?",
                    [datetime.now(timezone.utc), run_id])
        raise

    notes = (f"stage=snapshot; snapshots={len(todo)}; rows={total_rows}; "
             f"skipped_not_in_dim_player={total_skipped}")
    con.execute("UPDATE ingest_runs SET status='success', finished_at=?, rows_affected=?, notes=? WHERE run_id=?",
                [datetime.now(timezone.utc), total_rows, notes, run_id])
    con.close()
    print(f"Done. {total_rows} rows upserted from {len(todo)} snapshot(s).")


if __name__ == "__main__":
    run(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB_PATH,
        sys.argv[2] if len(sys.argv) > 2 else None)
