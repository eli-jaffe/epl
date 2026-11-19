"""Take a market snapshot from the live FPL API (stage B: extract).

One bootstrap-static call. It carries every player's *current* price,
ownership and this-gameweek transfer flow, which the API only ever shows as
of now — so a day we skip can't be recovered later (unlike stage C, which can
be re-run any time). Each run lands in its own timestamped directory under
config.LIVE_SNAPSHOT_RAW_DIR; the timestamp is the snapshot's identity
(`snapshot_at` / `source_ref` in fact_player_snapshot).

Separate from live_api.py's tree on purpose: load_live.py reads the newest
directory under raw/live_api/ and would choke on a snapshot-only directory.
Writes into "<ts>.partial" and renames at the end, like the other extracts.
Run from ingestion/ with the venv active:
    python live_snapshot.py
"""
import sys
from datetime import datetime, timezone

import requests

import config
from live_api import derive_season_id, get_json, make_session, save


def run() -> dict:
    session = make_session()
    fetched_at = datetime.now(timezone.utc)
    final_dest = config.LIVE_SNAPSHOT_RAW_DIR / fetched_at.strftime("%Y%m%dT%H%M%SZ")
    dest = final_dest.with_name(final_dest.name + ".partial")

    print("Fetching bootstrap-static...")
    bootstrap = get_json(session, "bootstrap-static/")
    save(dest, "bootstrap-static.json", bootstrap)

    manifest = {"source": "live_api", "stage": "snapshot", "fetched_at": fetched_at.isoformat(),
                "season_id": derive_season_id(bootstrap["events"])}
    save(dest, "manifest.json", manifest)
    dest.rename(final_dest)
    print(f"  season {manifest['season_id']}, {len(bootstrap['elements'])} players")
    print(f"Landed in {final_dest}")
    return manifest


if __name__ == "__main__":
    try:
        run()
    except (requests.RequestException, RuntimeError) as exc:
        print(f"Error during snapshot ingestion: {exc}", file=sys.stderr)
        sys.exit(1)
