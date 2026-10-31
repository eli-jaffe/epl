"""Pull current-season FPL data from the live public API.

Pure extract step, mirroring github_historical.py: fetch bootstrap-static,
fixtures, and event/<gw>/live for every gameweek that has started, save the
raw JSON under config.LIVE_RAW_DATA_DIR/<fetch_timestamp>/, and write a
manifest.json. The separate load step does the transform and DB writes.

Every run lands in its own timestamped directory (unlike the GitHub landing
zone, which keeps only the latest snapshot) because there's no commit SHA to
identify a live pull — the timestamp *is* its source_ref.
"""
import json
import sys
import time
from datetime import datetime, timezone

import requests

import config


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": config.USER_AGENT})
    return session


def get_json(session: requests.Session, path: str):
    """GET with retry/backoff — the API is busier around gameweek deadlines."""
    url = f"{config.FPL_API_BASE}/{path}"
    for attempt in range(1, config.LIVE_MAX_ATTEMPTS + 1):
        try:
            resp = session.get(url, timeout=config.REQUEST_TIMEOUT_SECONDS)
            resp.raise_for_status()
            return resp.json()
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
            retryable = not isinstance(exc, requests.HTTPError) or exc.response.status_code >= 500 \
                or exc.response.status_code == 429
            if attempt == config.LIVE_MAX_ATTEMPTS or not retryable:
                raise
            wait = 2 ** attempt
            print(f"  {path}: {exc}; retrying in {wait}s ({attempt}/{config.LIVE_MAX_ATTEMPTS})")
            time.sleep(wait)


def derive_season_id(events: list[dict]) -> str:
    """The API only exposes the current season, so derive its id from the
    first gameweek's deadline: EPL seasons start in August."""
    first = datetime.fromisoformat(events[0]["deadline_time"].replace("Z", "+00:00"))
    start_year = first.year if first.month >= 7 else first.year - 1
    return f"{start_year}-{(start_year + 1) % 100:02d}"


def save(dest_dir, name: str, data) -> None:
    (dest_dir / name).parent.mkdir(parents=True, exist_ok=True)
    (dest_dir / name).write_text(json.dumps(data))


def run() -> dict:
    session = make_session()
    fetched_at = datetime.now(timezone.utc)
    stamp = fetched_at.isoformat()
    dest = config.LIVE_RAW_DATA_DIR / fetched_at.strftime("%Y%m%dT%H%M%SZ")

    print("Fetching bootstrap-static...")
    bootstrap = get_json(session, "bootstrap-static/")
    save(dest, "bootstrap-static.json", bootstrap)

    events = bootstrap["events"]
    season_id = derive_season_id(events)
    # A gameweek has live data once it's current or finished; later ones are empty.
    gameweeks = [e["id"] for e in events if e["is_current"] or e["finished"]]
    print(f"  season {season_id}, {len(bootstrap['elements'])} players, "
          f"gameweeks with data: {gameweeks[:1]}..{gameweeks[-1:]}")

    print("Fetching fixtures...")
    save(dest, "fixtures.json", get_json(session, "fixtures/"))

    for gw in gameweeks:
        save(dest, f"event_live/{gw}.json", get_json(session, f"event/{gw}/live/"))
        print(f"  event/{gw}/live: ok")

    manifest = {
        "source": "live_api",
        "fetched_at": stamp,
        "season_id": season_id,
        "gameweeks": gameweeks,
    }
    save(dest, "manifest.json", manifest)
    print(f"\nManifest written to {dest / 'manifest.json'}")
    return manifest


if __name__ == "__main__":
    try:
        run()
    except requests.RequestException as exc:
        print(f"HTTP error during live ingestion: {exc}", file=sys.stderr)
        sys.exit(1)
