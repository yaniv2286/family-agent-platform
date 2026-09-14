"""Generic agent control-plane API.

Provides:
  - agent registry, health, capabilities
  - telemetry snapshot + refresh
  - analytics proxy/cache
  - permitted command dispatch with audit
  - signed inbound webhook handler with replay protection
"""
import json
import re
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_connector import AgentConnector, verify_webhook_signature
from database import (
    AgentCapability,
    AgentHealthSnapshot,
    AgentTelemetryCache,
    ExternalCommandAudit,
    RegisteredAgent,
    SessionLocal,
    WebhookReceipt,
    get_db,
)
from rbac import Role, get_current_role, require_permission

router = APIRouter(prefix="/api/control", tags=["Control Plane"])

AGENT_ID_PATH = "{agent_id}"
_AGENT_LOGS = deque(maxlen=2000)
_LOG_AGENT_NAMES = {
    "fitness_agent": {"fitness"},
    "tutors": {"koko_math", "koko_english"},
    "orchestrator": {"orchestrator"},
    "scheduler": {"orchestrator"},
    "agents.messenger_agent": {"messenger"},
    "agents.data_agent": {"data"},
    "agents.nutrition_agent": {"nutrition"},
    "nutrition_agent": {"nutrition"},
    "memory_manager": {"data"},
    "memory_layer": {"data"},
}
_LOG_SECRET_RE = re.compile(r"(?i)(secret|token|password|api[_-]?key|authorization)(\s*[=:]\s*)(\S+)")
_LOG_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_LOG_AGENT_TERMS = {
    "koko_math": ("/api/tutor/", "tutors:"),
    "koko_english": ("/api/tutor/", "tutors:"),
    "fitness": ("/api/fitness/", "fitness_agent:"),
    "nutrition": ("nutrition_agent", "nutrition:"),
    "orchestrator": ("orchestrator:", "scheduler:", "/api/orchestrator/"),
    "messenger": ("messenger_agent", "/api/messenger/"),
    "data": ("data_agent", "memory_layer:", "memory_manager:"),
}


def _sanitize_log_message(value: str) -> str:
    value = _LOG_SECRET_RE.sub(r"\1\2[REDACTED]", str(value))
    value = _LOG_EMAIL_RE.sub("[EMAIL]", value)
    return value.replace("\r", " ").replace("\n", " ")[:2000]


def capture_agent_log(message):
    record = message.record
    name = str(record.get("name") or "")
    agents = set()
    for module, mapped_agents in _LOG_AGENT_NAMES.items():
        if name == module or name.startswith(module + "."):
            agents.update(mapped_agents)
    path = str(record.get("extra", {}).get("request_path", ""))
    if path.startswith("/api/tutor"):
        agents.update({"koko_math", "koko_english"})
    elif path.startswith("/api/fitness"):
        agents.add("fitness")
    elif path.startswith("/api/orchestrator"):
        agents.add("orchestrator")
    _AGENT_LOGS.append({
        "timestamp": record["time"].isoformat(),
        "level": record["level"].name,
        "module": name,
        "function": record.get("function"),
        "message": _sanitize_log_message(record.get("message", "")),
        "agents": sorted(agents),
    })


def _persisted_agent_logs(agent_id: str, limit: int) -> list[dict]:
    terms = _LOG_AGENT_TERMS.get(agent_id, ())
    if not terms:
        return []
    paths = sorted(Path("logs").glob("tutor_app_*.log"), key=lambda path: path.stat().st_mtime, reverse=True)
    matched = []
    for path in paths[:3]:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if any(term.lower() in line.lower() for term in terms):
                parts = line.split(" | ")
                matched.append({
                    "timestamp": parts[0] if parts else None,
                    "level": parts[3].strip() if len(parts) > 3 else "INFO",
                    "module": agent_id,
                    "function": None,
                    "message": _sanitize_log_message(line),
                    "agents": [agent_id],
                })
                if len(matched) >= limit:
                    return list(reversed(matched))
    return list(reversed(matched))


def _json_loads(value: Optional[str]) -> dict:
    try:
        return json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class AgentRegister(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=80)
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None
    kind: str = Field("generic")
    remote_url: Optional[str] = None
    secret_env: Optional[str] = None
    capabilities: List[str] = Field(default_factory=list)


class HealthUpdate(BaseModel):
    status: str = Field(..., min_length=1, max_length=50)
    detail: Optional[dict] = None


class CommandRequest(BaseModel):
    command_type: str = Field(..., min_length=1, max_length=100)
    payload: Optional[dict] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _get_agent(db: AsyncSession, agent_id: str) -> RegisteredAgent:
    agent = (
        await db.execute(select(RegisteredAgent).where(RegisteredAgent.agent_id == agent_id))
    ).scalar_one_or_none()
    if not agent:
        raise HTTPException(404, detail="agent not found")
    return agent


def _agent_dict(agent: RegisteredAgent, capabilities: Optional[List[str]] = None) -> dict:
    return {
        "agent_id": agent.agent_id,
        "name": agent.name,
        "description": agent.description,
        "kind": agent.kind,
        "status": agent.status,
        "registered_at": agent.registered_at,
        "remote_url": agent.remote_url,
        "capabilities": capabilities or [],
    }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
@router.get("/agents")
async def list_agents(request: Request, db: AsyncSession = Depends(get_db)):
    """List registered agents. Filtered by caller role."""
    role = get_current_role(request)
    allowed = None
    if role == Role.CHILD:
        allowed = {"koko_math", "koko_english", "fitness"}

    agents = (await db.execute(select(RegisteredAgent))).scalars().all()
    result = []
    for agent in agents:
        if allowed is not None and agent.agent_id not in allowed:
            continue
        caps = (
            await db.execute(
                select(AgentCapability.capability).where(AgentCapability.agent_id == agent.agent_id)
            )
        ).scalars().all()
        result.append(_agent_dict(agent, list(caps)))
    return {"agents": result}


@router.post("/agents")
async def register_agent(
    request: Request,
    data: AgentRegister,
    db: AsyncSession = Depends(get_db),
):
    """Register a new agent (admin only)."""
    require_permission(request, "control", "admin")
    existing = (
        await db.execute(select(RegisteredAgent).where(RegisteredAgent.agent_id == data.agent_id))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(409, detail="agent already registered")

    agent = RegisteredAgent(
        agent_id=data.agent_id,
        name=data.name,
        description=data.description,
        kind=data.kind,
        remote_url=data.remote_url,
        secret_env=data.secret_env,
        config_json="{}",
    )
    db.add(agent)
    await db.flush()

    for cap in set(data.capabilities):
        db.add(AgentCapability(agent_id=data.agent_id, capability=cap, meta_json="{}"))

    await db.commit()
    await db.refresh(agent)
    return _agent_dict(agent, list(set(data.capabilities)))


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    require_permission(request, agent_id, "read")
    agent = await _get_agent(db, agent_id)
    caps = (
        await db.execute(
            select(AgentCapability.capability).where(AgentCapability.agent_id == agent_id)
        )
    ).scalars().all()
    return _agent_dict(agent, list(caps))


@router.get("/agents/{agent_id}/capabilities")
async def get_capabilities(agent_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    require_permission(request, agent_id, "read")
    await _get_agent(db, agent_id)
    rows = (
        await db.execute(
            select(AgentCapability).where(AgentCapability.agent_id == agent_id)
        )
    ).scalars().all()
    return [
        {"capability": r.capability, "meta": _json_loads(r.meta_json), "created_at": r.created_at}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
@router.get("/agents/{agent_id}/logs")
async def get_agent_logs(agent_id: str, request: Request, limit: int = 100, db: AsyncSession = Depends(get_db)):
    role = get_current_role(request)
    if role not in {Role.ADMIN, Role.PARENT}:
        raise HTTPException(403, detail="agent logs require parent or admin access")
    agent = await _get_agent(db, agent_id)
    limit = max(1, min(limit, 500))
    if agent.is_builtin:
        live_logs = [item for item in _AGENT_LOGS if agent_id in item["agents"]]
        persisted_logs = _persisted_agent_logs(agent_id, limit)
        return {"agent_id": agent_id, "source": "local", "logs": (persisted_logs + live_logs)[-limit:]}
    try:
        response = await AgentConnector(agent_id).send("GET", "/logs/recent")
        data = response.get("data", response) if isinstance(response, dict) else {}
        lines = data.get("lines", []) if isinstance(data, dict) else []
        logs = [{"timestamp": None, "level": "INFO", "module": agent_id, "function": None, "message": _sanitize_log_message(line)} for line in lines[-limit:]]
        return {"agent_id": agent_id, "source": "remote", "logs": logs}
    except Exception as exc:
        return {"agent_id": agent_id, "source": "unavailable", "logs": [], "error": _sanitize_log_message(str(exc))}


@router.get("/agents/{agent_id}/health")
async def get_health(agent_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    require_permission(request, agent_id, "read")
    await _get_agent(db, agent_id)
    row = (
        await db.execute(
            select(AgentHealthSnapshot)
            .where(AgentHealthSnapshot.agent_id == agent_id)
            .order_by(AgentHealthSnapshot.recorded_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not row:
        return {"status": "unknown", "detail": {}, "recorded_at": None}
    return {"status": row.status, "detail": _json_loads(row.detail_json), "recorded_at": row.recorded_at}


@router.post("/agents/{agent_id}/health")
async def post_health(
    agent_id: str,
    data: HealthUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Submit a health snapshot (admin can submit for any agent; parent for family)."""
    require_permission(request, agent_id, "command")
    await _get_agent(db, agent_id)
    snapshot = AgentHealthSnapshot(
        agent_id=agent_id,
        status=data.status,
        detail_json=json.dumps(data.detail or {}, ensure_ascii=False),
    )
    db.add(snapshot)
    await db.commit()
    await db.refresh(snapshot)
    return {"status": snapshot.status, "recorded_at": snapshot.recorded_at}


# ---------------------------------------------------------------------------
# Telemetry snapshot / refresh
# ---------------------------------------------------------------------------
@router.get("/agents/{agent_id}/snapshot")
async def get_snapshot(agent_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    require_permission(request, agent_id, "read")
    await _get_agent(db, agent_id)
    row = (
        await db.execute(
            select(AgentTelemetryCache).where(AgentTelemetryCache.agent_id == agent_id)
        )
    ).scalar_one_or_none()
    if not row:
        return {"agent_id": agent_id, "snapshot": {}, "cached_at": None}
    return {"agent_id": agent_id, "snapshot": _json_loads(row.snapshot_json), "cached_at": row.cached_at}


@router.post("/agents/{agent_id}/snapshot/refresh")
async def refresh_snapshot(
    agent_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Refresh cached telemetry by calling the agent over the signed connector."""
    role = require_permission(request, agent_id, "read")
    await _get_agent(db, agent_id)

    try:
        snapshot = await AgentConnector(agent_id).fetch_telemetry()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, detail=f"failed to refresh telemetry: {exc}") from exc

    cached = (
        await db.execute(select(AgentTelemetryCache).where(AgentTelemetryCache.agent_id == agent_id))
    ).scalar_one_or_none()
    if cached:
        cached.snapshot_json = json.dumps(snapshot, ensure_ascii=False)
        cached.cached_at = datetime.utcnow()
        cached.expires_at = datetime.utcnow() + timedelta(minutes=5)
    else:
        cached = AgentTelemetryCache(
            agent_id=agent_id,
            snapshot_json=json.dumps(snapshot, ensure_ascii=False),
            cached_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(minutes=5),
        )
        db.add(cached)
    await db.commit()
    await db.refresh(cached)
    return {"agent_id": agent_id, "snapshot": snapshot, "cached_at": cached.cached_at}


# ---------------------------------------------------------------------------
# Analytics proxy / cache
# ---------------------------------------------------------------------------
@router.get("/agents/{agent_id}/analytics")
async def get_analytics(agent_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Return analytics for an agent, proxying from the agent and updating cache."""
    role = require_permission(request, agent_id, "read")
    if role == Role.CHILD and agent_id in {"neuraltrader", "volatilityhunter"}:
        raise HTTPException(403, detail="children cannot access trading analytics")
    await _get_agent(db, agent_id)

    try:
        analytics = await AgentConnector(agent_id).send("GET", "/analytics")
    except HTTPException:
        # Fall back to cached snapshot if available.
        cached = (
            await db.execute(select(AgentTelemetryCache).where(AgentTelemetryCache.agent_id == agent_id))
        ).scalar_one_or_none()
        if cached:
            analytics = _json_loads(cached.snapshot_json)
        else:
            analytics = {}
    except Exception:
        cached = (
            await db.execute(select(AgentTelemetryCache).where(AgentTelemetryCache.agent_id == agent_id))
        ).scalar_one_or_none()
        analytics = _json_loads(cached.snapshot_json) if cached else {}

    return {"agent_id": agent_id, "analytics": analytics}


# ---------------------------------------------------------------------------
# Permitted commands
# ---------------------------------------------------------------------------
@router.post("/agents/{agent_id}/commands")
async def send_command(
    agent_id: str,
    data: CommandRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    issued_by_user: Optional[str] = None,
):
    """Issue an external command to an agent, subject to RBAC and audit."""
    role = require_permission(request, agent_id, "command")
    await _get_agent(db, agent_id)

    audit = ExternalCommandAudit(
        agent_id=agent_id,
        command_type=data.command_type,
        payload_json=json.dumps(data.payload or {}, ensure_ascii=False),
        issued_by_role=role,
        issued_by_user=issued_by_user,
        status="pending",
    )
    db.add(audit)
    await db.flush()

    try:
        result = await AgentConnector(agent_id).send_command(data.command_type, data.payload or {})
        audit.status = "completed"
        audit.response_json = json.dumps(result, ensure_ascii=False)
    except HTTPException as exc:
        audit.status = "failed"
        audit.response_json = json.dumps({"error": exc.detail}, ensure_ascii=False)
        await db.commit()
        raise
    except Exception as exc:
        audit.status = "failed"
        audit.response_json = json.dumps({"error": str(exc)}, ensure_ascii=False)
        await db.commit()
        raise HTTPException(502, detail=f"command failed: {exc}") from exc
    finally:
        audit.completed_at = datetime.utcnow()

    await db.commit()
    await db.refresh(audit)
    return {"command_id": audit.id, "agent_id": agent_id, "status": audit.status, "result": result}


# ---------------------------------------------------------------------------
# Signed inbound webhook with replay protection
# ---------------------------------------------------------------------------
@router.post("/webhooks/inbound")
async def inbound_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Receive a signed webhook from an external agent."""
    agent_id = request.headers.get("X-Agent-ID")
    timestamp_header = request.headers.get("X-Agent-Timestamp")
    signature = request.headers.get("X-Agent-Signature")
    nonce = request.headers.get("X-Nonce")

    if not agent_id or not timestamp_header or not signature or not nonce:
        raise HTTPException(401, detail="missing signature headers")

    try:
        timestamp = int(timestamp_header)
    except (TypeError, ValueError):
        raise HTTPException(401, detail="invalid timestamp")

    body = await request.body()
    path = request.url.path

    if not verify_webhook_signature(agent_id, timestamp, path, body, signature):
        raise HTTPException(401, detail="invalid signature")

    await _get_agent(db, agent_id)

    existing = (
        await db.execute(select(WebhookReceipt).where(WebhookReceipt.nonce == nonce))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(409, detail="replay: nonce already used")

    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except ValueError:
        payload = {"raw": body.decode("utf-8", errors="replace")}

    receipt = WebhookReceipt(
        agent_id=agent_id,
        nonce=nonce,
        signature=signature,
        payload_json=json.dumps(payload, ensure_ascii=False),
    )
    db.add(receipt)
    await db.commit()
    await db.refresh(receipt)
    return {"status": "ok", "receipt_id": receipt.id}


# ---------------------------------------------------------------------------
# Role introspection
# ---------------------------------------------------------------------------
@router.get("/me")
async def whoami(request: Request):
    role = getattr(request.state, "role", None)
    return {"role": role or "unknown"}
