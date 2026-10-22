"""FastAPI app entrypoint -- see docs/agent_ui_architecture_plan.md.

Skeleton: auth routes are real (fastapi-users + self-hosted Postgres), the
chat route is a stub pending the agent loop. Run locally with:

    docker compose up -d          # starts local Postgres
    cp .env.example .env          # then fill in real values
    uvicorn agent.main:app --reload
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from agent.api.admin import router as admin_router
from agent.api.chat import router as chat_router
from agent.auth.schemas import UserCreate, UserRead, UserUpdate
from agent.auth.users import auth_backend, fastapi_users
from agent.db.postgres import Base, engine
from agent.observability import mark_stale_queries

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 60


async def _sweep_stale_queries() -> None:
    # First pass runs immediately (catches queries orphaned by the previous
    # process), then once a minute for hung tasks in this one.
    while True:
        try:
            count = await mark_stale_queries()
            if count:
                logger.warning("Marked %d stale running quer%s interrupted", count, "y" if count == 1 else "ies")
        except Exception:  # noqa: BLE001 -- a failed sweep must never take the app down
            logger.exception("Stale-query sweep failed")
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Dev convenience only -- create tables directly instead of migrating.
    # Swap for Alembic migrations before this touches any real user data.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sweep = asyncio.create_task(_sweep_stale_queries())
    yield
    sweep.cancel()


app = FastAPI(title="EPL Fantasy Agent", lifespan=lifespan)

app.include_router(
    fastapi_users.get_auth_router(auth_backend), prefix="/auth/jwt", tags=["auth"]
)
app.include_router(
    fastapi_users.get_register_router(UserRead, UserCreate), prefix="/auth", tags=["auth"]
)
app.include_router(
    fastapi_users.get_users_router(UserRead, UserUpdate), prefix="/users", tags=["users"]
)
app.include_router(chat_router)
app.include_router(admin_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
