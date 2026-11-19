"""Backfill weekly market data from the live FPL API (stage C: element-summary).

Pure extract step, like live_api.py. event/<gw>/live (stage A) has no weekly
price, ownership or transfer numbers; only element-summary/<player_id> has
them, one call per player. This fetches that page for every player in the
newest complete live_api pull and lands the raw JSON inside that same pull,
under element_summary/<player_id>.json, so each backfill sits next to the
bootstrap-static.json whose id -> code map it needs. (A separate directory
would break load_live.latest_pull_dir(), which expects bootstrap-static.json
in the newest directory.)

Like live_api.py, files go into "element_summary.partial" and the directory
is renamed only after every player succeeded, so a directory without the
suffix is always complete. Run from ingestion/ with the venv active:
    python live_element_summary.py
"""
import json
import sys
from datetime import datetime, timezone

import requests

import config
from live_api import get_json, make_session, save


def latest_pull_dir():
    """Newest complete pull (same rule as load_live.latest_pull_dir; kept
    separate so this extract step doesn't import the DB loader)."""
    pulls = sorted(d for d in config.LIVE_RAW_DATA_DIR.iterdir()
                   if d.is_dir() and not d.name.endswith(".partial"))
    if not pulls:
        raise FileNotFoundError(f"no complete live_api pull in {config.LIVE_RAW_DATA_DIR}; run live_api.py first")
    return pulls[-1]


def run() -> dict:
    pull = latest_pull_dir()
    final_dest = pull / "element_summary"
    dest = pull / "element_summary.partial"
    if final_dest.exists():
        raise RuntimeError(f"{final_dest} already exists; this pull is already backfilled "
                           "(run live_api.py for a fresh pull, or delete the directory to redo it)")
    if dest.exists():   # leftover from a failed run
        for f in dest.iterdir():
            f.unlink()
        dest.rmdir()

    session = make_session()
    fetched_at = datetime.now(timezone.utc)
    player_ids = [e["id"] for e in json.loads((pull / "bootstrap-static.json").read_text())["elements"]]
    print(f"Pull {pull.name}: fetching element-summary for {len(player_ids)} players...")

    for n, pid in enumerate(player_ids, 1):
        save(dest, f"{pid}.json", get_json(session, f"element-summary/{pid}/"))
        if n % 100 == 0:
            print(f"  {n}/{len(player_ids)}")

    manifest = {"source": "live_api", "stage": "element_summary", "fetched_at": fetched_at.isoformat(),
                "players": len(player_ids)}
    save(dest, "manifest.json", manifest)
    dest.rename(final_dest)
    print(f"Done. Landed in {final_dest}")
    return manifest


if __name__ == "__main__":
    try:
        run()
    except (requests.RequestException, RuntimeError, FileNotFoundError) as exc:
        print(f"Error during element-summary ingestion: {exc}", file=sys.stderr)
        sys.exit(1)
