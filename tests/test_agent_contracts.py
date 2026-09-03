"""Contract tests for cross-repository agent adapters.

These tests mock the outbound HTTP layer and independently recompute the
expected HMAC signatures for each adapter contract. They assert exact request
paths, header names/values, analytics aggregation, NeuralTrader command
mapping, and VolatilityHunter monitor-only RBAC.
"""
import hashlib
import hmac
import json
import re
from typing import Any, Dict, List

import httpx
import pytest
from fastapi import HTTPException

import agent_connector


class _MockResponse:
    def __init__(self, json_data: Any):
        self._json = json_data
        self.content = json.dumps(json_data).encode("utf-8")
        self.status_code = 200
        self.text = json.dumps(json_data)

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


class _RecordingAsyncClient:
    """Minimal stand-in for httpx.AsyncClient that records every request."""

    requests: List[Dict[str, Any]] = []

    def __init__(self, *args, **kwargs):
        pass

    async def request(self, method: str, url: str, *, content=None, headers=None, **kwargs):
        _RecordingAsyncClient.requests.append(
            {"method": method, "url": url, "content": content, "headers": dict(headers or {})}
        )
        return _MockResponse({"ok": True})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


@pytest.fixture(autouse=True)
def _reset_recording():
    _RecordingAsyncClient.requests.clear()
    yield
    _RecordingAsyncClient.requests.clear()


@pytest.fixture(autouse=True)
def _mock_httpx(monkeypatch):
    monkeypatch.setattr(agent_connector.httpx, "AsyncClient", _RecordingAsyncClient)


# ── helpers ──────────────────────────────────────────────────────────────────
def _parse_request(req: Dict[str, Any]) -> Dict[str, Any]:
    """Decompose a recorded request into method, path, headers, body."""
    url = req["url"]
    path = "/" + url.split("/", 3)[3] if "/" in url[8:] else "/"
    return {
        "method": req["method"],
        "url": url,
        "path": path,
        "headers": req["headers"],
        "body": req["content"] or b"",
    }


def _neural_expected_signature(secret: str, method: str, path: str, timestamp: int, body: bytes) -> str:
    body_hash = hashlib.sha256(body or b"").hexdigest()
    base = f"{method.upper()}\n{path}\n{timestamp}\n{body_hash}"
    return hmac.new(secret.encode("utf-8"), base.encode("utf-8"), hashlib.sha256).hexdigest()


def _hunter_expected_signature(secret: str, method: str, path: str, timestamp: int, body: bytes) -> str:
    body_hash = hashlib.sha256(body).hexdigest() if body else ""
    base = f"{timestamp}|{method.upper()}|{path}|{body_hash}"
    return hmac.new(secret.encode("utf-8"), base.encode("utf-8"), hashlib.sha256).hexdigest()


def _default_expected_signature(secret: str, path: str, timestamp: int, body: bytes) -> str:
    message = f"{timestamp}:{path}:".encode("utf-8") + (body or b"")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


# ── NeuralTrader contract ────────────────────────────────────────────────────
async def test_neuraltrader_single_request_path_and_headers(monkeypatch):
    monkeypatch.setenv("AGENT_NEURALTRADER_URL", "https://neural.example.com")
    monkeypatch.setenv("AGENT_NEURALTRADER_SECRET", "neural-secret")

    connector = agent_connector.AgentConnector("neuraltrader")
    await connector.send("GET", "/status")

    assert len(_RecordingAsyncClient.requests) == 1
    req = _parse_request(_RecordingAsyncClient.requests[0])
    assert req["method"] == "GET"
    assert req["path"] == "/v1/status"

    headers = req["headers"]
    assert "X-Agent-Timestamp" in headers
    assert "X-Agent-Signature" in headers
    assert "X-Agent-ID" not in headers
    timestamp = int(headers["X-Agent-Timestamp"])

    expected = _neural_expected_signature("neural-secret", "GET", "/v1/status", timestamp, b"")
    assert hmac.compare_digest(headers["X-Agent-Signature"], expected)


async def test_neuraltrader_signed_post_body(monkeypatch):
    monkeypatch.setenv("AGENT_NEURALTRADER_URL", "https://neural.example.com")
    monkeypatch.setenv("AGENT_NEURALTRADER_SECRET", "neural-secret")

    connector = agent_connector.AgentConnector("neuraltrader")
    payload = {"mode": "paper", "confirm": True}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await connector.send("POST", "/control/command", payload)

    req = _parse_request(_RecordingAsyncClient.requests[0])
    assert req["method"] == "POST"
    assert req["path"] == "/v1/control/command"
    assert req["body"] == body

    headers = req["headers"]
    timestamp = int(headers["X-Agent-Timestamp"])
    expected = _neural_expected_signature(
        "neural-secret", "POST", "/v1/control/command", timestamp, body
    )
    assert hmac.compare_digest(headers["X-Agent-Signature"], expected)


async def test_neuraltrader_command_mapping(monkeypatch):
    monkeypatch.setenv("AGENT_NEURALTRADER_URL", "https://neural.example.com")
    monkeypatch.setenv("AGENT_NEURALTRADER_SECRET", "neural-secret")

    connector = agent_connector.AgentConnector("neuraltrader")
    result = await connector.send_command("paper", {"symbol": "AAPL", "qty": 1})
    assert result["ok"] is True

    assert len(_RecordingAsyncClient.requests) == 1
    req = _parse_request(_RecordingAsyncClient.requests[0])
    assert req["method"] == "POST"
    assert req["path"] == "/v1/control/command"

    body = json.loads(req["body"])
    assert body["mode"] == "paper"
    assert body["confirm"] is True


async def test_neuraltrader_rejects_disallowed_command(monkeypatch):
    monkeypatch.setenv("AGENT_NEURALTRADER_URL", "https://neural.example.com")
    monkeypatch.setenv("AGENT_NEURALTRADER_SECRET", "neural-secret")

    connector = agent_connector.AgentConnector("neuraltrader")
    with pytest.raises(HTTPException) as exc_info:
        await connector.send_command("trade", {"symbol": "AAPL"})
    assert exc_info.value.status_code == 403
    assert "trade" in exc_info.value.detail


async def test_neuraltrader_fetch_telemetry_aggregates_endpoints(monkeypatch):
    monkeypatch.setenv("AGENT_NEURALTRADER_URL", "https://neural.example.com")
    monkeypatch.setenv("AGENT_NEURALTRADER_SECRET", "neural-secret")

    connector = agent_connector.AgentConnector("neuraltrader")
    snapshot = await connector.fetch_telemetry()

    assert snapshot["agent_id"] == "neuraltrader"
    assert "telemetry" in snapshot
    assert "endpoints" in snapshot
    assert set(snapshot["telemetry"].keys()) == set(connector._profile.telemetry_endpoints)
    for endpoint in connector._profile.telemetry_endpoints:
        assert f"https://neural.example.com/v1{endpoint}" in {r["url"] for r in _RecordingAsyncClient.requests}


async def test_neuraltrader_fetch_analytics_aggregates_endpoints(monkeypatch):
    monkeypatch.setenv("AGENT_NEURALTRADER_URL", "https://neural.example.com")
    monkeypatch.setenv("AGENT_NEURALTRADER_SECRET", "neural-secret")

    connector = agent_connector.AgentConnector("neuraltrader")
    snapshot = await connector.fetch_analytics()

    assert snapshot["agent_id"] == "neuraltrader"
    assert set(snapshot["analytics"].keys()) == set(connector._profile.analytics_endpoints)
    for endpoint in connector._profile.analytics_endpoints:
        assert f"https://neural.example.com/v1{endpoint}" in {r["url"] for r in _RecordingAsyncClient.requests}


# ── VolatilityHunter contract ──────────────────────────────────────────────
async def test_volatilityhunter_single_request_path_and_headers(monkeypatch):
    monkeypatch.setenv("AGENT_VOLATILITYHUNTER_URL", "https://hunter.example.com")
    monkeypatch.setenv("AGENT_VOLATILITYHUNTER_SECRET", "hunter-secret")

    connector = agent_connector.AgentConnector("volatilityhunter")
    await connector.send("GET", "/risk")

    assert len(_RecordingAsyncClient.requests) == 1
    req = _parse_request(_RecordingAsyncClient.requests[0])
    assert req["method"] == "GET"
    assert req["path"] == "/api/v1/risk"

    headers = req["headers"]
    assert headers.get("X-Agent-ID") == "volatilityhunter"
    assert "X-Timestamp" in headers
    assert "X-Signature" in headers
    timestamp = int(headers["X-Timestamp"])

    expected = _hunter_expected_signature("hunter-secret", "GET", "/api/v1/risk", timestamp, b"")
    assert hmac.compare_digest(headers["X-Signature"], expected)


async def test_volatilityhunter_empty_body_uses_empty_hash(monkeypatch):
    monkeypatch.setenv("AGENT_VOLATILITYHUNTER_URL", "https://hunter.example.com")
    monkeypatch.setenv("AGENT_VOLATILITYHUNTER_SECRET", "hunter-secret")

    connector = agent_connector.AgentConnector("volatilityhunter")
    await connector.send("GET", "/health")

    req = _parse_request(_RecordingAsyncClient.requests[0])
    assert req["body"] == b""
    timestamp = int(req["headers"]["X-Timestamp"])
    expected = _hunter_expected_signature(
        "hunter-secret", "GET", "/api/v1/health", timestamp, b""
    )
    assert hmac.compare_digest(req["headers"]["X-Signature"], expected)


async def test_volatilityhunter_post_with_body(monkeypatch):
    monkeypatch.setenv("AGENT_VOLATILITYHUNTER_URL", "https://hunter.example.com")
    monkeypatch.setenv("AGENT_VOLATILITYHUNTER_SECRET", "hunter-secret")

    connector = agent_connector.AgentConnector("volatilityhunter")
    payload = {"limit": 10}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await connector.send("POST", "/alerts", payload)

    req = _parse_request(_RecordingAsyncClient.requests[0])
    assert req["method"] == "POST"
    assert req["path"] == "/api/v1/alerts"
    assert req["body"] == body

    timestamp = int(req["headers"]["X-Timestamp"])
    expected = _hunter_expected_signature(
        "hunter-secret", "POST", "/api/v1/alerts", timestamp, body
    )
    assert hmac.compare_digest(req["headers"]["X-Signature"], expected)


async def test_volatilityhunter_monitor_only_rejects_commands(monkeypatch):
    monkeypatch.setenv("AGENT_VOLATILITYHUNTER_URL", "https://hunter.example.com")
    monkeypatch.setenv("AGENT_VOLATILITYHUNTER_SECRET", "hunter-secret")

    connector = agent_connector.AgentConnector("volatilityhunter")
    with pytest.raises(HTTPException) as exc_info:
        await connector.send_command("rebalance", {"target": "SPY"})
    assert exc_info.value.status_code == 403
    assert "does not accept commands" in exc_info.value.detail


async def test_volatilityhunter_fetch_telemetry_aggregates_endpoints(monkeypatch):
    monkeypatch.setenv("AGENT_VOLATILITYHUNTER_URL", "https://hunter.example.com")
    monkeypatch.setenv("AGENT_VOLATILITYHUNTER_SECRET", "hunter-secret")

    connector = agent_connector.AgentConnector("volatilityhunter")
    snapshot = await connector.fetch_telemetry()

    assert snapshot["agent_id"] == "volatilityhunter"
    assert set(snapshot["telemetry"].keys()) == set(connector._profile.telemetry_endpoints)
    for endpoint in connector._profile.telemetry_endpoints:
        assert f"https://hunter.example.com/api/v1{endpoint}" in {r["url"] for r in _RecordingAsyncClient.requests}


async def test_volatilityhunter_fetch_analytics_aggregates_endpoints(monkeypatch):
    monkeypatch.setenv("AGENT_VOLATILITYHUNTER_URL", "https://hunter.example.com")
    monkeypatch.setenv("AGENT_VOLATILITYHUNTER_SECRET", "hunter-secret")

    connector = agent_connector.AgentConnector("volatilityhunter")
    snapshot = await connector.fetch_analytics()

    assert snapshot["agent_id"] == "volatilityhunter"
    assert set(snapshot["analytics"].keys()) == set(connector._profile.analytics_endpoints)
    for endpoint in connector._profile.analytics_endpoints:
        assert f"https://hunter.example.com/api/v1{endpoint}" in {r["url"] for r in _RecordingAsyncClient.requests}


# ── Default / future-agent contract ──────────────────────────────────────────
async def test_default_agent_retains_central_contract(monkeypatch):
    monkeypatch.setenv("AGENT_FUTURE_AGENT_URL", "https://future.example.com")
    monkeypatch.setenv("AGENT_FUTURE_AGENT_SECRET", "future-secret")

    connector = agent_connector.AgentConnector("future_agent")
    payload = {"action": "ping"}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await connector.send("POST", "/commands/ping", payload)

    assert len(_RecordingAsyncClient.requests) == 1
    req = _parse_request(_RecordingAsyncClient.requests[0])
    assert req["method"] == "POST"
    assert req["path"] == "/commands/ping"
    assert req["body"] == body

    headers = req["headers"]
    assert headers.get("X-Agent-ID") == "future_agent"
    assert "X-Agent-Timestamp" in headers
    assert "X-Agent-Signature" in headers
    timestamp = int(headers["X-Agent-Timestamp"])

    expected = _default_expected_signature("future-secret", "/commands/ping", timestamp, body)
    assert hmac.compare_digest(headers["X-Agent-Signature"], expected)


async def test_default_agent_command_uses_commands_path(monkeypatch):
    monkeypatch.setenv("AGENT_FUTURE_AGENT_URL", "https://future.example.com")
    monkeypatch.setenv("AGENT_FUTURE_AGENT_SECRET", "future-secret")

    connector = agent_connector.AgentConnector("future_agent")
    result = await connector.send_command("do_thing", {"foo": "bar"})
    assert result["ok"] is True

    req = _parse_request(_RecordingAsyncClient.requests[0])
    assert req["method"] == "POST"
    assert req["path"] == "/commands/do_thing"
    assert json.loads(req["body"]) == {"foo": "bar"}
