"""Settings, loaded from environment / .env -- see .env.example."""
from __future__ import annotations

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DUCKDB_PATH = REPO_ROOT / "db" / "epl.duckdb"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://epl:epl@localhost:5432/epl"
    jwt_secret: str = "change-me-dev-only"
    anthropic_api_key: str | None = None
    duckdb_path: Path = DEFAULT_DUCKDB_PATH

    @field_validator("duckdb_path", mode="before")
    @classmethod
    def _empty_means_default(cls, v: object) -> object:
        # .env.example documents DUCKDB_PATH= (blank) as "use the default" --
        # but pydantic-settings treats a *present*, empty env var as an
        # explicit override, not "unset", so it silently replaced the real
        # default with an empty path (which resolved to the repo root
        # directory itself and broke every DuckDB connection). Found by
        # actually running agent/observability_sync.py, 2026-09-18.
        if v in (None, ""):
            return DEFAULT_DUCKDB_PATH
        return v


settings = Settings()
