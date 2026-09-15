"""Shared constants for data ingestion."""
from pathlib import Path

# --- GitHub historical data source (vaastav/Fantasy-Premier-League) ---
GITHUB_OWNER = "vaastav"
GITHUB_REPO = "Fantasy-Premier-League"
GITHUB_API_BASE = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
GITHUB_RAW_BASE = f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/master"

# Per-season files we pull. merged_gw.csv already contains every gameweek
# concatenated, so we don't need to pull gw1.csv..gw38.csv individually.
SEASON_FILES = [
    "players_raw.csv",
    "cleaned_players.csv",
    "player_idlist.csv",
    "teams.csv",
    "fixtures.csv",
    "gws/merged_gw.csv",
]

# Files at the repo's data/ root that aren't season-scoped.
GLOBAL_FILES = [
    "master_team_list.csv",
]

# Landing zone lives outside the app project entirely — see MEMORY/architecture
# discussion: it's both the pipeline's read source and the disaster-recovery
# backup if the upstream repo ever disappears (most-recent-snapshot only, no
# retained history).
RAW_DATA_DIR = Path.home() / "epl-data" / "raw" / "github"

REQUEST_TIMEOUT_SECONDS = 30
USER_AGENT = "epl-fantasy-app-data-ingestion (personal project)"
