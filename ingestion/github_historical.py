"""Pull historical FPL data from the public vaastav/Fantasy-Premier-League repo.

This is a pure extract step: it discovers season directories, downloads the
known CSVs for each into the external landing zone (config.RAW_DATA_DIR), and
writes a manifest.json recording the commit SHA the pull came from. It talks
only to public, unauthenticated GitHub endpoints (api.github.com and
raw.githubusercontent.com) — no token, no git, no credentials.

The separate load step (not this script) reads the landing zone + manifest
and is responsible for populating the DuckDB tables' source/source_ref/
loaded_at columns and the ingest_runs log.
"""
import json
import sys
from datetime import datetime, timezone

import requests

import config


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": config.USER_AGENT})
    return session


def get_latest_commit_sha(session: requests.Session) -> str:
    resp = session.get(f"{config.GITHUB_API_BASE}/commits/master",
                        timeout=config.REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.json()["sha"]


def discover_seasons(session: requests.Session) -> list[str]:
    resp = session.get(f"{config.GITHUB_API_BASE}/contents/data",
                        timeout=config.REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    items = resp.json()
    seasons = sorted(item["name"] for item in items if item["type"] == "dir")
    return seasons


def download_file(session: requests.Session, url: str, dest_path) -> bool:
    """Download url to dest_path. Returns True on success, False on 404
    (some older seasons are missing certain files — that's expected, not
    a fatal error)."""
    resp = session.get(url, timeout=config.REQUEST_TIMEOUT_SECONDS)
    if resp.status_code == 404:
        return False
    resp.raise_for_status()
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(resp.content)
    return True


def run() -> dict:
    session = make_session()
    fetched_at = datetime.now(timezone.utc).isoformat()

    print("Fetching latest commit SHA...")
    commit_sha = get_latest_commit_sha(session)
    print(f"  {commit_sha}")

    print("Discovering season directories...")
    seasons = discover_seasons(session)
    print(f"  found {len(seasons)} seasons: {', '.join(seasons)}")

    results = {"seasons": {}, "global_files": {}}

    for season in seasons:
        season_result = {}
        for relative_file in config.SEASON_FILES:
            url = f"{config.GITHUB_RAW_BASE}/data/{season}/{relative_file}"
            dest = config.RAW_DATA_DIR / season / relative_file
            ok = download_file(session, url, dest)
            season_result[relative_file] = "ok" if ok else "missing"
            status = "ok" if ok else "MISSING (skipped)"
            print(f"  [{season}] {relative_file}: {status}")
        results["seasons"][season] = season_result

    for relative_file in config.GLOBAL_FILES:
        url = f"{config.GITHUB_RAW_BASE}/data/{relative_file}"
        dest = config.RAW_DATA_DIR / "_global" / relative_file
        ok = download_file(session, url, dest)
        results["global_files"][relative_file] = "ok" if ok else "missing"
        print(f"  [global] {relative_file}: {'ok' if ok else 'MISSING (skipped)'}")

    manifest = {
        "source": "github_repo",
        "repo": f"{config.GITHUB_OWNER}/{config.GITHUB_REPO}",
        "commit_sha": commit_sha,
        "fetched_at": fetched_at,
        "seasons": seasons,
        "files": results,
    }
    manifest_path = config.RAW_DATA_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"\nManifest written to {manifest_path}")

    return manifest


if __name__ == "__main__":
    try:
        run()
    except requests.HTTPError as exc:
        print(f"HTTP error during ingestion: {exc}", file=sys.stderr)
        sys.exit(1)
