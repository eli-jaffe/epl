"""Service-account user for MCP calls that bypass the HTTP auth layer.

agent.loop.run_agent_query requires a real user_id -- agent_query.user_id
has an FK constraint against the users table (see
[[project-epl-fantasy-pipeline]]'s 2026-09-18 end-to-end run notes: a fake
random user_id was correctly rejected). ask_epl_agent (agent/mcp_server.py)
has no JWT/session to pull a user from, since MCP clients don't go through
/auth/jwt, so it attributes every call to one fixed, lazily-created service
user instead of skipping attribution or requiring MCP clients to log in.
"""
from __future__ import annotations

import uuid

from fastapi_users import exceptions
from fastapi_users.db import SQLAlchemyUserDatabase

from agent.auth.models import User
from agent.auth.schemas import UserCreate
from agent.auth.users import UserManager
from agent.db.postgres import async_session_maker

SERVICE_USER_EMAIL = "mcp-service@epl.internal"

_service_user_id: uuid.UUID | None = None


async def get_or_create_service_user_id() -> uuid.UUID:
    """Memoized per-process: one Postgres round-trip on the first call, none
    after.
    """
    global _service_user_id
    if _service_user_id is not None:
        return _service_user_id

    async with async_session_maker() as session:
        manager = UserManager(SQLAlchemyUserDatabase(session, User))
        try:
            user = await manager.get_by_email(SERVICE_USER_EMAIL)
        except exceptions.UserNotExists:
            user = await manager.create(
                UserCreate(email=SERVICE_USER_EMAIL, password=uuid.uuid4().hex, is_verified=True),
                safe=False,
            )

    _service_user_id = user.id
    return _service_user_id
