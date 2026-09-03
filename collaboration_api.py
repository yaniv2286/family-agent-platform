import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import AgentAction, AgentEvent, ParentDirective, get_db
from memory_manager import memory_manager

router = APIRouter(prefix="/api/collaboration", tags=["Collaboration"])


class DirectiveLifecycleRequest(BaseModel):
    status: str


def _json(value):
    try:
        return json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}


@router.get("/context/{child_id}")
async def inspect_context(child_id: int, domain: str = None):
    return await memory_manager.get_context(child_id, domain)


@router.get("/directives/{child_id}")
async def active_directives(child_id: int):
    rows = await memory_manager.get_active_directives(child_id=child_id)
    return [{"id": row.id, "message": row.message, "subject": row.subject,
             "priority": row.priority, "scope": row.scope, "status": row.status,
             "child_name": row.child_name, "created_at": row.created_at} for row in rows]


@router.patch("/directives/{directive_id}")
async def update_directive_lifecycle(
    directive_id: int, data: DirectiveLifecycleRequest, db: AsyncSession = Depends(get_db)
):
    if data.status not in {"completed", "cancelled"}:
        raise HTTPException(400, "status must be completed or cancelled")
    directive = await db.get(ParentDirective, directive_id)
    if not directive:
        raise HTTPException(404, "directive not found")
    if directive.status != "active":
        raise HTTPException(409, "directive is no longer active")
    directive.status = data.status
    directive.updated_at = datetime.utcnow()
    if data.status == "completed":
        directive.completed_at = datetime.utcnow()
    else:
        directive.cancelled_at = datetime.utcnow()
    await db.commit()
    await db.refresh(directive)
    return {"id": directive.id, "status": directive.status}


@router.get("/events/{child_id}")
async def inspect_events(child_id: int, limit: int = 50, db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(AgentEvent).where(AgentEvent.child_id == child_id)
                             .order_by(AgentEvent.created_at.desc()).limit(min(limit, 100)))).scalars().all()
    return [{"id": row.id, "type": row.event_type, "source_agent": row.source_agent,
             "payload": _json(row.payload_json), "status": row.status,
             "created_at": row.created_at} for row in rows]


@router.get("/actions/{child_id}")
async def inspect_actions(child_id: int, limit: int = 50, db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(AgentAction).where(AgentAction.child_id == child_id)
                             .order_by(AgentAction.created_at.desc()).limit(min(limit, 100)))).scalars().all()
    return [{"id": row.id, "event_id": row.event_id, "agent": row.agent_name,
             "action": row.action_type, "status": row.status,
             "detail": _json(row.detail_json), "created_at": row.created_at} for row in rows]
