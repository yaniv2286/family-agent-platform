"""Generic, HMAC-signed connector to remote agents.

The connector uses environment-configured per-agent URLs and secrets:

  AGENT_<AGENT_ID>_URL
  AGENT_<AGENT_ID>_SECRET

or, when dashes are present:

  AGENT_<UPPER_SNAKE>_URL
  AGENT_<UPPER_SNAKE>_SECRET

Each registered agent may follow a different contract. Built-in profiles:

* neuraltrader: paths under /v1, headers X-Agent-Timestamp/X-Agent-Signature,
  signature = HMAC-SHA256("METHOD\\nPATH\\nTIMESTAMP\\nSHA256(body)").
  Commands map allowed modes (fetch/paper/report) to POST /v1/control/command
  with confirm=true.

* volatilityhunter: paths under /api/v1, headers X-Agent-ID/X-Timestamp/X-Signature,
  signature = HMAC-SHA256("timestamp|METHOD|path|bodyhash") where an empty body
  yields an empty body-hash string. This adapter is read-only: commands are rejected.

* default / future agents retain the original central contract:
  paths under /, headers X-Agent-ID/X-Agent-Timestamp/X-Agent-Signature,
  signature = HMAC-SHA256("timestamp:path:body").

SSRF is prevented by only allowing requests to the base URL configured for the
agent, strict agent-id validation, short timeouts, and response-size limits.
"""
import dataclasses
import hmac
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urljoin

import httpx
from fastapi import HTTPException
from loguru import logger
from sqlalchemy import select

from database import RegisteredAgent, SessionLocal

AGENT_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
MAX_BODY_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT = 10.0
SIGNATURE_TTL_SECONDS = 300


@dataclasses.dataclass(frozen=True)
class AgentProfile:
    """Outbound contract profile for a registered agent."""

    name: str
    prefix: str
    timestamp_header: str
    signature_header: str
    id_header: Optional[str]
    sign: Callable[[str, str, str, int, bytes], str]
    telemetry_endpoints: List[str]
    analytics_endpoints: List[str]
    command_path: Optional[str]
    allowed_commands: Optional[frozenset]
    command_builder: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]]


# ---------------------------------------------------------------------------
# Signature schemes
# ---------------------------------------------------------------------------
def _default_sign(secret: str, method: str, path: str, timestamp: int, body: bytes) -> str:
    message = f"{timestamp}:{path}:".encode("utf-8") + (body or b"")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _neural_sign(secret: str, method: str, path: str, timestamp: int, body: bytes) -> str:
    body_hash = hashlib.sha256(body or b"").hexdigest()
    base = f"{method.upper()}\n{path}\n{timestamp}\n{body_hash}"
    return hmac.new(secret.encode("utf-8"), base.encode("utf-8"), hashlib.sha256).hexdigest()


def _hunter_sign(secret: str, method: str, path: str, timestamp: int, body: bytes) -> str:
    # Empty body must produce an empty hash string, matching VolatilityHunter.
    body_hash = hashlib.sha256(body).hexdigest() if body else ""
    base = f"{timestamp}|{method.upper()}|{path}|{body_hash}"
    return hmac.new(secret.encode("utf-8"), base.encode("utf-8"), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Command payload builders
# ---------------------------------------------------------------------------
def _neural_command(command_type: str, _payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"mode": command_type, "confirm": True}


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------
_NEURAL_PROFILE = AgentProfile(
    name="neuraltrader",
    prefix="/v1",
    timestamp_header="X-Agent-Timestamp",
    signature_header="X-Agent-Signature",
    id_header=None,
    sign=_neural_sign,
    telemetry_endpoints=[
        "/health",
        "/status",
        "/portfolio",
        "/alerts",
        "/model/summary",
        "/regime/summary",
    ],
    analytics_endpoints=[
        "/status",
        "/portfolio",
        "/positions",
        "/performance/history",
        "/signals",
        "/orders/trades",
    ],
    command_path="/v1/control/command",
    allowed_commands=frozenset({"fetch", "paper", "report"}),
    command_builder=_neural_command,
)

_HUNTER_PROFILE = AgentProfile(
    name="volatilityhunter",
    prefix="/api/v1",
    timestamp_header="X-Timestamp",
    signature_header="X-Signature",
    id_header="X-Agent-ID",
    sign=_hunter_sign,
    telemetry_endpoints=[
        "/health",
        "/status",
        "/portfolio/totals",
        "/risk",
        "/alerts",
    ],
    analytics_endpoints=[
        "/status",
        "/portfolio/totals",
        "/positions",
        "/performance",
        "/risk",
    ],
    command_path=None,
    allowed_commands=None,
    command_builder=None,
)

_DEFAULT_PROFILE = AgentProfile(
    name="default",
    prefix="",
    timestamp_header="X-Agent-Timestamp",
    signature_header="X-Agent-Signature",
    id_header="X-Agent-ID",
    sign=_default_sign,
    telemetry_endpoints=[],
    analytics_endpoints=[],
    command_path="/commands/{command_type}",
    allowed_commands=None,
    command_builder=None,
)

AGENT_PROFILES: Dict[str, AgentProfile] = {
    "neuraltrader": _NEURAL_PROFILE,
    "volatilityhunter": _HUNTER_PROFILE,
}

# Built-in agents that run inside the same process as the Koko backend.
# When no AGENT_<ID>_URL is configured, the connector dispatches to a local
# snapshot instead of making an outbound HTTP request.
BUILTIN_AGENT_IDS: frozenset = frozenset({"koko_math", "koko_english", "fitness", "nutrition"})


def _profile_for(agent_id: str) -> AgentProfile:
    return AGENT_PROFILES.get(agent_id.lower(), _DEFAULT_PROFILE)


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------
def _env_var(agent_id: str, suffix: str) -> Optional[str]:
    """Return env value for AGENT_{UPPER_SNAKE}_{suffix}."""
    normalized = agent_id.upper().replace("-", "_")
    return os.getenv(f"AGENT_{normalized}_{suffix}")


def _agent_secret(agent_id: str) -> Optional[str]:
    """Load the shared secret for an agent from the environment."""
    secret = _env_var(agent_id, "SECRET")
    if secret:
        return secret
    # legacy fallback: a global webhook secret may be used for built-ins
    return os.getenv("AGENT_DEFAULT_SECRET")


def _agent_url(agent_id: str) -> Optional[str]:
    return _env_var(agent_id, "URL")


def _build_signature(secret: str, timestamp: int, path: str, body: bytes) -> str:
    """Legacy default signature primitive used by webhook verification."""
    return _default_sign(secret, "POST", path, timestamp, body)


def _verify_signature(secret: str, timestamp: int, path: str, body: bytes, signature: str) -> bool:
    expected = _build_signature(secret, timestamp, path, body)
    return hmac.compare_digest(expected, signature)


def _normalize_path(prefix: str, path: str) -> str:
    if path.startswith(prefix):
        return path
    if not path.startswith("/"):
        path = "/" + path
    return prefix + path


class AgentConnector:
    """Signs and dispatches requests to a registered remote agent."""

    def __init__(self, agent_id: str):
        if not AGENT_ID_RE.fullmatch(agent_id):
            raise ValueError(f"invalid agent id {agent_id}")
        self.agent_id = agent_id
        self._profile = _profile_for(agent_id)

    async def _resolve_config(self) -> tuple[str, str]:
        """Return (remote_url, secret). Raises ValueError if not configured."""
        remote_url = _agent_url(self.agent_id)
        secret = _agent_secret(self.agent_id)

        if not remote_url:
            async with SessionLocal() as db:
                agent = (
                    await db.execute(
                        select(RegisteredAgent).where(RegisteredAgent.agent_id == self.agent_id)
                    )
                ).scalar_one_or_none()
                if agent and agent.remote_url:
                    remote_url = agent.remote_url

        if not remote_url:
            raise ValueError(f"remote URL not configured for agent {self.agent_id}")
        if not secret:
            raise ValueError(f"secret not configured for agent {self.agent_id}")

        return remote_url, secret

    def _build_url(self, remote_url: str, path: str) -> str:
        """Join remote base URL with a relative path. SSRF hardening."""
        if path.lower().startswith(("http://", "https://")):
            raise ValueError("absolute URLs are not allowed")
        if not path.startswith("/"):
            path = "/" + path
        base = remote_url.rstrip("/")
        full = urljoin(base + "/", path.lstrip("/"))
        # Ensure the constructed URL stays under the configured base.
        if not full.startswith(base):
            raise ValueError("SSRF: constructed URL escapes agent base")
        return full

    async def _send_local(self, method: str, path: str, payload: Optional[dict] = None) -> dict:
        """Return a local snapshot for a built-in agent instead of an HTTP call."""
        return {
            "agent_id": self.agent_id,
            "mode": "local",
            "message": (
                f"Built-in agent '{self.agent_id}' runs in-process. "
                "Use the direct API endpoints for chat, fitness, etc."
            ),
            "received": {"method": method, "path": path, "payload": payload},
        }

    async def _send_single(
        self,
        method: str,
        path: str,
        payload: Optional[dict] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> dict:
        """Send a single signed request to the agent and return parsed JSON."""
        # Local built-in agents: skip HTTP when no remote URL is configured.
        if self.agent_id in BUILTIN_AGENT_IDS and not _agent_url(self.agent_id):
            return await self._send_local(method, path, payload)

        remote_url, secret = await self._resolve_config()
        profile = self._profile
        path = _normalize_path(profile.prefix, path)
        full_url = self._build_url(remote_url, path)

        body = b""
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(body) > MAX_BODY_BYTES:
            raise ValueError("payload exceeds maximum body size")

        timestamp = int(time.time())
        signature = profile.sign(secret, method, path, timestamp, body)

        headers: Dict[str, str] = {
            profile.timestamp_header: str(timestamp),
            profile.signature_header: signature,
            "Content-Type": "application/json",
        }
        if profile.id_header:
            headers[profile.id_header] = self.agent_id

        limits = httpx.Limits(max_keepalive_connections=1, max_connections=2)
        async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
            try:
                response = await client.request(method, full_url, content=body, headers=headers)
                response.raise_for_status()
            except httpx.TimeoutException as exc:
                logger.warning("agent request timed out", agent_id=self.agent_id, path=path)
                raise HTTPException(504, detail="agent request timed out") from exc
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    "agent returned error",
                    agent_id=self.agent_id,
                    path=path,
                    status=exc.response.status_code,
                )
                raise HTTPException(
                    502, detail=f"agent returned {exc.response.status_code}"
                ) from exc
            except httpx.RequestError as exc:
                logger.warning("agent request failed", agent_id=self.agent_id, path=path, error=str(exc))
                raise HTTPException(502, detail="agent request failed") from exc

        if len(response.content) > MAX_RESPONSE_BYTES:
            raise HTTPException(413, detail="agent response too large")

        try:
            return response.json()
        except ValueError:
            return {"raw": response.text}

    async def send(
        self,
        method: str,
        path: str,
        payload: Optional[dict] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> dict:
        """Send a signed request to the agent and return parsed JSON.

        The special paths ``/telemetry`` and ``/analytics`` aggregate the
        agent's actual endpoint set into a generic structured snapshot.
        """
        if path == "/telemetry":
            return await self.fetch_telemetry()
        if path == "/analytics":
            return await self.fetch_analytics()
        return await self._send_single(method, path, payload, timeout)

    async def _aggregate(self, endpoints: List[str], snapshot_key: str) -> dict:
        """Call each endpoint and merge results into a structured snapshot."""
        results: Dict[str, Any] = {}
        for endpoint in endpoints:
            try:
                results[endpoint] = await self.send("GET", endpoint)
            except Exception as exc:  # pragma: no cover - defensive
                results[endpoint] = {"error": str(exc)}
        return {
            "agent_id": self.agent_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            snapshot_key: results,
            "endpoints": endpoints,
        }

    async def fetch_telemetry(self) -> dict:
        """Refresh telemetry by aggregating the agent's actual endpoint set."""
        profile = self._profile
        if not profile.telemetry_endpoints:
            return await self._send_single("GET", "/telemetry")
        return await self._aggregate(profile.telemetry_endpoints, "telemetry")

    async def fetch_analytics(self) -> dict:
        """Refresh analytics by aggregating the agent's actual endpoint set."""
        profile = self._profile
        if not profile.analytics_endpoints:
            return await self._send_single("GET", "/analytics")
        return await self._aggregate(profile.analytics_endpoints, "analytics")

    async def send_command(self, command_type: str, payload: dict) -> dict:
        """Dispatch a command to the agent using its profile-specific contract."""
        profile = self._profile
        if profile.command_path is None:
            raise HTTPException(403, detail=f"agent {self.agent_id} does not accept commands")
        if profile.allowed_commands is not None and command_type not in profile.allowed_commands:
            raise HTTPException(
                403,
                detail=f"command {command_type} not permitted for agent {self.agent_id}",
            )
        if profile.command_builder:
            body = profile.command_builder(command_type, payload or {})
        else:
            body = payload or {}
        path = profile.command_path.format(command_type=command_type)
        return await self.send("POST", path, body)


def verify_webhook_signature(agent_id: str, timestamp: int, path: str, body: bytes, signature: str) -> bool:
    """Verify an inbound webhook signature from an agent (default contract)."""
    secret = _agent_secret(agent_id)
    if not secret:
        return False
    now = int(time.time())
    if abs(now - timestamp) > SIGNATURE_TTL_SECONDS:
        return False
    return _verify_signature(secret, timestamp, path, body, signature)


def can_store_receipt(nonce: str) -> bool:
    """Return True only if nonce looks non-empty and reasonable."""
    return bool(nonce and len(nonce) <= 256)
