"""Mocked tests for the generic agent control plane.

No real external HTTP calls are made: the AgentConnector is replaced with a
fake implementation and the HMAC/webhook primitives are exercised directly.
"""
import hmac
import hashlib
import json
import time

import pytest
from sqlalchemy import select

import agent_connector
import control_plane
from database import (
    AgentHealthSnapshot,
    AgentTelemetryCache,
    ExternalCommandAudit,
    RegisteredAgent,
    SessionLocal,
    WebhookReceipt,
)

APP_PIN = "test-pin-1234"
ADMIN_PIN = "admin-secret-99"
CHILD_PIN = "child-secret-11"


# ── helpers / fixtures ─────────────────────────────────────────────────────
class FakeConnector:
    """Drop-in replacement for AgentConnector in tests.

    Mirrors the per-agent contract behaviour of the real connector without
    making HTTP calls.
    """

    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self.profile = agent_connector._profile_for(agent_id)

    async def send(self, method: str, path: str, payload=None, timeout: float = 10.0):
        if path == "/telemetry":
            return await self.fetch_telemetry()
        if path == "/analytics":
            return await self.fetch_analytics()
        return {"agent": self.agent_id, "path": path, "method": method, "payload": payload}

    async def fetch_telemetry(self):
        return {
            "agent_id": self.agent_id,
            "timestamp": "fake",
            "telemetry": {ep: {"ok": True} for ep in self.profile.telemetry_endpoints},
            "endpoints": self.profile.telemetry_endpoints,
        }

    async def fetch_analytics(self):
        return {
            "agent_id": self.agent_id,
            "timestamp": "fake",
            "analytics": {ep: {"PnL": 123.45} for ep in self.profile.analytics_endpoints},
            "endpoints": self.profile.analytics_endpoints,
        }

    async def send_command(self, command_type: str, payload: dict):
        if self.profile.command_path is None:
            raise HTTPException(403, detail=f"agent {self.agent_id} does not accept commands")
        if (
            self.profile.allowed_commands is not None
            and command_type not in self.profile.allowed_commands
        ):
            raise HTTPException(403, detail=f"command {command_type} not permitted")
        if self.profile.command_builder:
            body = self.profile.command_builder(command_type, payload or {})
        else:
            body = payload or {}
        path = self.profile.command_path.format(command_type=command_type)
        return {"ack": True, "agent": self.agent_id, "command": path, "payload": body}


@pytest.fixture
def fake_connector(monkeypatch):
    monkeypatch.setattr(control_plane, "AgentConnector", FakeConnector)
    yield FakeConnector


@pytest.fixture
def admin_headers(monkeypatch):
    monkeypatch.setenv("ADMIN_PIN", ADMIN_PIN)
    return {"x-app-pin": ADMIN_PIN}


@pytest.fixture
def parent_headers():
    return {"x-app-pin": APP_PIN}


@pytest.fixture
def child_headers(monkeypatch):
    monkeypatch.setenv("CHILD_PIN", CHILD_PIN)
    return {"x-app-pin": CHILD_PIN}


# ── role / PIN resolution ────────────────────────────────────────────────────
async def test_me_returns_parent_for_app_pin(client):
    resp = await client.get("/api/control/me", headers={"x-app-pin": APP_PIN})
    assert resp.status_code == 200
    assert resp.json()["role"] == "parent"


async def test_me_returns_admin_for_admin_pin(client, admin_headers):
    resp = await client.get("/api/control/me", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["role"] == "admin"


async def test_me_returns_child_for_child_pin(client, child_headers):
    resp = await client.get("/api/control/me", headers=child_headers)
    assert resp.status_code == 200
    assert resp.json()["role"] == "child"


async def test_control_endpoints_require_pin(client):
    resp = await client.get("/api/control/agents")
    assert resp.status_code == 401


# ── registry ─────────────────────────────────────────────────────────────────
async def test_list_agents_includes_built_ins(client, parent_headers):
    resp = await client.get("/api/control/agents", headers=parent_headers)
    assert resp.status_code == 200
    ids = {a["agent_id"] for a in resp.json()["agents"]}
    assert ids == {"koko_math", "koko_english", "fitness", "nutrition", "neuraltrader", "volatilityhunter", "orchestrator", "messenger", "data"}


async def test_child_only_sees_permitted_agents(client, child_headers):
    resp = await client.get("/api/control/agents", headers=child_headers)
    assert resp.status_code == 200
    ids = {a["agent_id"] for a in resp.json()["agents"]}
    assert ids == {"koko_math", "koko_english", "fitness"}


async def test_register_agent_is_admin_only(client, parent_headers, admin_headers):
    body = {
        "agent_id": "future_agent",
        "name": "Future Agent",
        "kind": "generic",
        "capabilities": ["think"],
    }
    assert (await client.post("/api/control/agents", json=body, headers=parent_headers)).status_code == 403
    resp = await client.post("/api/control/agents", json=body, headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["agent_id"] == "future_agent"


async def test_get_agent_details(client, parent_headers):
    resp = await client.get("/api/control/agents/neuraltrader", headers=parent_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["kind"] == "trading"
    assert "analytics" in data["capabilities"]


async def test_capabilities_listed(client, parent_headers):
    resp = await client.get("/api/control/agents/fitness/capabilities", headers=parent_headers)
    assert resp.status_code == 200
    caps = {c["capability"] for c in resp.json()}
    assert "plan" in caps


# ── health snapshots ───────────────────────────────────────────────────────
async def test_get_health_unknown_by_default(client, parent_headers):
    resp = await client.get("/api/control/agents/fitness/health", headers=parent_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "unknown"


async def test_post_health_is_audited(client, parent_headers):
    resp = await client.post(
        "/api/control/agents/fitness/health",
        json={"status": "healthy", "detail": {"cpu": 12}},
        headers=parent_headers,
    )
    assert resp.status_code == 200
    async with SessionLocal() as db:
        row = (await db.execute(select(AgentHealthSnapshot))).scalars().first()
    assert row.status == "healthy"


async def test_child_cannot_post_health_for_nutrition(client, child_headers):
    resp = await client.post(
        "/api/control/agents/nutrition/health",
        json={"status": "healthy"},
        headers=child_headers,
    )
    assert resp.status_code == 403


# ── telemetry snapshot / refresh / analytics cache ───────────────────────────
async def test_refresh_snapshot_caches_telemetry(client, parent_headers, fake_connector):
    resp = await client.post("/api/control/agents/neuraltrader/snapshot/refresh", headers=parent_headers)
    assert resp.status_code == 200
    assert resp.json()["snapshot"]["agent_id"] == "neuraltrader"
    async with SessionLocal() as db:
        row = (await db.execute(
            select(AgentTelemetryCache).where(AgentTelemetryCache.agent_id == "neuraltrader")
        )).scalar_one()
    cached = json.loads(row.snapshot_json)
    assert cached["agent_id"] == "neuraltrader"


async def test_snapshot_endpoint_returns_cached_data(client, parent_headers, fake_connector):
    await client.post("/api/control/agents/volatilityhunter/snapshot/refresh", headers=parent_headers)
    resp = await client.get("/api/control/agents/volatilityhunter/snapshot", headers=parent_headers)
    assert resp.status_code == 200
    assert resp.json()["snapshot"]["agent_id"] == "volatilityhunter"


async def test_analytics_proxy_returns_trading_data(client, parent_headers, fake_connector):
    resp = await client.get("/api/control/agents/neuraltrader/analytics", headers=parent_headers)
    assert resp.status_code == 200
    data = resp.json()["analytics"]
    assert data["agent_id"] == "neuraltrader"
    assert data["analytics"]  # aggregated endpoint snapshot


async def test_child_cannot_read_trading_analytics(client, child_headers, fake_connector):
    resp = await client.get("/api/control/agents/neuraltrader/analytics", headers=child_headers)
    assert resp.status_code == 403


# ── permitted commands / RBAC ──────────────────────────────────────────────
async def test_parent_can_command_family_agent(client, parent_headers, fake_connector):
    resp = await client.post(
        "/api/control/agents/fitness/commands",
        json={"command_type": "plan", "payload": {"user_id": 1}},
        headers=parent_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"
    async with SessionLocal() as db:
        audit = (await db.execute(select(ExternalCommandAudit))).scalars().first()
    assert audit.issued_by_role == "parent"
    assert audit.agent_id == "fitness"


async def test_child_can_command_own_tutor(client, child_headers, fake_connector):
    resp = await client.post(
        "/api/control/agents/koko_math/commands",
        json={"command_type": "chat", "payload": {"message": "hi"}},
        headers=child_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"


async def test_child_cannot_command_nutrition(client, child_headers, fake_connector):
    resp = await client.post(
        "/api/control/agents/nutrition/commands",
        json={"command_type": "recommend", "payload": {}},
        headers=child_headers,
    )
    assert resp.status_code == 403


async def test_neuraltrader_commands_are_admin_only(client, parent_headers, admin_headers, fake_connector):
    resp = await client.post(
        "/api/control/agents/neuraltrader/commands",
        json={"command_type": "paper", "payload": {"symbol": "AAPL", "qty": 1}},
        headers=parent_headers,
    )
    assert resp.status_code == 403

    resp = await client.post(
        "/api/control/agents/neuraltrader/commands",
        json={"command_type": "paper", "payload": {"symbol": "AAPL", "qty": 1}},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"


async def test_volatilityhunter_is_monitor_only(client, admin_headers, fake_connector):
    resp = await client.post(
        "/api/control/agents/volatilityhunter/commands",
        json={"command_type": "alert", "payload": {}},
        headers=admin_headers,
    )
    assert resp.status_code == 403


# ── signed inbound webhook with replay protection ────────────────────────────
def _webhook_signature(secret: str, timestamp: int, body: bytes) -> str:
    message = f"{timestamp}:/api/control/webhooks/inbound:".encode("utf-8") + body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


async def test_inbound_webhook_requires_valid_signature(client, monkeypatch):
    monkeypatch.setenv("AGENT_NEURALTRADER_SECRET", "webhook-secret")
    body = b'{"event":"trade"}'
    ts = int(time.time())
    sig = _webhook_signature("webhook-secret", ts, body)
    resp = await client.post(
        "/api/control/webhooks/inbound",
        content=body,
        headers={
            "x-app-pin": APP_PIN,
            "X-Agent-ID": "neuraltrader",
            "X-Agent-Timestamp": str(ts),
            "X-Agent-Signature": sig,
            "X-Nonce": "nonce-abc-1",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_replay_nonce_is_rejected(client, monkeypatch):
    monkeypatch.setenv("AGENT_NEURALTRADER_SECRET", "webhook-secret")
    body = b'{"event":"trade"}'
    ts = int(time.time())
    sig = _webhook_signature("webhook-secret", ts, body)
    headers = {
        "x-app-pin": APP_PIN,
        "X-Agent-ID": "neuraltrader",
        "X-Agent-Timestamp": str(ts),
        "X-Agent-Signature": sig,
        "X-Nonce": "nonce-abc-2",
        "Content-Type": "application/json",
    }
    first = await client.post("/api/control/webhooks/inbound", content=body, headers=headers)
    assert first.status_code == 200
    second = await client.post("/api/control/webhooks/inbound", content=body, headers=headers)
    assert second.status_code == 409


async def test_invalid_webhook_signature_is_rejected(client, monkeypatch):
    monkeypatch.setenv("AGENT_NEURALTRADER_SECRET", "webhook-secret")
    resp = await client.post(
        "/api/control/webhooks/inbound",
        content=b'{}',
        headers={
            "x-app-pin": APP_PIN,
            "X-Agent-ID": "neuraltrader",
            "X-Agent-Timestamp": str(int(time.time())),
            "X-Agent-Signature": "bad-sig",
            "X-Nonce": "nonce-xyz",
        },
    )
    assert resp.status_code == 401


# ── connector SSRF / HMAC hardening ─────────────────────────────────────────
async def test_connector_rejects_absolute_path_ssrf(monkeypatch):
    monkeypatch.setenv("AGENT_FOO_URL", "https://agent.example.com")
    monkeypatch.setenv("AGENT_FOO_SECRET", "secret")
    connector = agent_connector.AgentConnector("foo")
    with pytest.raises(ValueError, match="absolute URLs"):
        connector._build_url("https://agent.example.com", "https://evil.com/abc")


async def test_connector_rejects_path_escaping_base(monkeypatch):
    monkeypatch.setenv("AGENT_FOO_URL", "https://agent.example.com/api")
    monkeypatch.setenv("AGENT_FOO_SECRET", "secret")
    connector = agent_connector.AgentConnector("foo")
    with pytest.raises(ValueError, match="SSRF"):
        connector._build_url("https://agent.example.com/api", "/../v1/admin")


async def test_connector_builds_valid_signed_url(monkeypatch):
    monkeypatch.setenv("AGENT_FOO_URL", "https://agent.example.com/")
    monkeypatch.setenv("AGENT_FOO_SECRET", "secret")
    connector = agent_connector.AgentConnector("foo")
    url = connector._build_url("https://agent.example.com/", "/commands/paper_trade")
    assert url == "https://agent.example.com/commands/paper_trade"


async def test_hmac_signature_is_verified(monkeypatch):
    monkeypatch.setenv("AGENT_FOO_SECRET", "super-secret")
    body = b'{"x":1}'
    ts = int(time.time())
    sig = agent_connector._build_signature("super-secret", ts, "/api/hook", body)
    assert agent_connector.verify_webhook_signature("foo", ts, "/api/hook", body, sig)
    assert not agent_connector.verify_webhook_signature("foo", ts, "/api/hook", body, "bad")
