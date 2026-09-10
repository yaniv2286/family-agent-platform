"""Dynamic loader for built-in agent routers.

After the database is initialized, this module reads the registered_agents
table, imports any built-in module that exposes a FastAPI router, and returns
it so main.py can include it without hard-coding every agent.
"""
import importlib
from typing import List

from loguru import logger
from sqlalchemy import select

from database import RegisteredAgent, SessionLocal


async def load_builtin_routers() -> List:
    """Return FastAPI routers for all active built-in agents with a module_path."""
    routers = []
    async with SessionLocal() as db:
        rows = (await db.execute(
            select(RegisteredAgent).where(
                RegisteredAgent.is_builtin == 1,
                RegisteredAgent.module_path.is_not(None),
                RegisteredAgent.status == "active",
            )
        )).scalars().all()

    for agent in rows:
        try:
            module = importlib.import_module(agent.module_path)
            attr = agent.router_attr or "router"
            router = getattr(module, attr, None)
            if router is None:
                logger.warning(
                    "Built-in agent module has no router attribute",
                    agent_id=agent.agent_id,
                    module_path=agent.module_path,
                    router_attr=attr,
                )
                continue
            routers.append(router)
            logger.info(
                "Loaded built-in agent router",
                agent_id=agent.agent_id,
                module_path=agent.module_path,
            )
        except Exception:
            logger.exception(
                "Failed to load built-in agent router",
                agent_id=agent.agent_id,
                module_path=agent.module_path,
            )
    return routers
