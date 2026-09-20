"""User table -- self-hosted Postgres via SQLAlchemy, not a third-party
identity service. See docs/agent_ui_architecture_plan.md's auth decision.
"""
from __future__ import annotations

from fastapi_users.db import SQLAlchemyBaseUserTableUUID

from agent.db.postgres import Base


class User(SQLAlchemyBaseUserTableUUID, Base):
    pass
