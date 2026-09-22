"""Bootstrap script: promote a user to superuser (admin) status.

Run as `python -m agent.auth.promote_admin <email>`. There's no self-serve
path to become an admin -- this is how you make your own account the first
one, directly against Postgres.
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from agent.auth.models import User
from agent.db.postgres import async_session_maker


async def promote(email: str) -> None:
    async with async_session_maker() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is None:
            print(f"No user found with email {email!r}.")
            return
        if user.is_superuser:
            print(f"{email} is already a superuser.")
            return
        user.is_superuser = True
        await session.commit()
        print(f"{email} promoted to superuser.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python -m agent.auth.promote_admin <email>")
        sys.exit(1)
    asyncio.run(promote(sys.argv[1]))
