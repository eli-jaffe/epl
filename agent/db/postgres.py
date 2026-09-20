"""Async Postgres session -- users/app state only (saved dashboards, chart
edits). NOT the analytical warehouse -- that's db/epl.duckdb, accessed via
agent/db/duckdb.py. See docs/agent_ui_architecture_plan.md's three-way split.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from agent.config import settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(settings.database_url)
async_session_maker = async_sessionmaker(engine, expire_on_commit=False)


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        yield session
