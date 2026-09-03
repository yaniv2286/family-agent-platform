"""Role-based access control for the generic agent control plane.

Supports deterministic role authentication from PIN headers:
  - ADMIN_PIN  -> admin
  - PARENT_PIN -> parent (falls back to legacy APP_PIN)
  - CHILD_PIN  -> child

Permission matrix (built-in agents):
  - admin: all agents, all actions.
  - parent: read all family/wellness/trading analytics; command family/wellness agents
            (koko_math, koko_english, fitness, nutrition). NeuralTrader paper
            commands are admin-only. VolatilityHunter is monitor-only.
  - child:  read/command only own permitted tutor/fitness agents
            (koko_math, koko_english, fitness).
  - volatilityhunter: monitor-only (no commands for any role, including admin).
  - neuraltrader: paper-trade commands are admin-only; analytics readable by
    parent and admin.
"""
import os
from typing import Optional

from fastapi import HTTPException, Request


class Role:
    ADMIN = "admin"
    PARENT = "parent"
    CHILD = "child"


_CHILD_AGENTS = {"koko_math", "koko_english", "fitness"}
_MONITOR_ONLY = {"volatilityhunter"}
_ADMIN_ONLY_COMMAND = {"neuraltrader"}


def resolve_role(pin: Optional[str]) -> Optional[str]:
    if not pin:
        return None
    if os.getenv("ADMIN_PIN") and pin == os.getenv("ADMIN_PIN"):
        return Role.ADMIN
    # Preserve legacy APP_PIN behaviour: when PARENT_PIN is not set, APP_PIN acts as the parent PIN.
    parent_pin = os.getenv("PARENT_PIN") or os.getenv("APP_PIN") or "1234"
    if pin == parent_pin:
        return Role.PARENT
    if os.getenv("CHILD_PIN") and pin == os.getenv("CHILD_PIN"):
        return Role.CHILD
    return None


def get_current_role(request: Request) -> str:
    role = getattr(request.state, "role", None)
    if not role:
        raise HTTPException(401, detail="Role not identified")
    return role


def can_read(role: str, agent_id: str) -> bool:
    if role == Role.ADMIN or role == Role.PARENT:
        return True
    if role == Role.CHILD:
        return agent_id in _CHILD_AGENTS
    return False


def can_command(role: str, agent_id: str, command_type: Optional[str] = None) -> bool:
    if agent_id in _MONITOR_ONLY:
        return False
    if role == Role.ADMIN:
        return True
    if role == Role.PARENT:
        if agent_id in _ADMIN_ONLY_COMMAND:
            return False
        return agent_id in _CHILD_AGENTS
    if role == Role.CHILD:
        return agent_id in _CHILD_AGENTS
    return False


def require_permission(request: Request, agent_id: str, action: str) -> str:
    role = get_current_role(request)
    allowed = False
    if action == "read":
        allowed = can_read(role, agent_id)
    elif action == "command":
        allowed = can_command(role, agent_id)
    elif action == "admin":
        allowed = role == Role.ADMIN
    else:
        raise HTTPException(400, detail=f"unknown action {action}")
    if not allowed:
        raise HTTPException(403, detail=f"role {role} cannot {action} agent {agent_id}")
    return role
