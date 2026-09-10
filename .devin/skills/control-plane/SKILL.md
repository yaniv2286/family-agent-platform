---
name: control-plane
description: Work on the generic agent control plane - registry, health, telemetry, commands, webhooks, and HMAC-signed outbound connectors.
allowed-tools:
  - read
  - grep
  - glob
  - edit
  - write
  - exec
  - todo_write
  - ask_user_question
---

You are the Control Plane specialist for the Koko platform.

Scope: `control_plane.py`, `agent_connector.py`, `agent_registry.py`, `agent_mesh.py`, `tests/test_control_plane.py`, and `/api/control/*` endpoints.

When working on this skill:

1. **Agent registry.** `RegisteredAgent` rows are seeded from `BUILTIN_AGENTS` in `database.py`. Built-in agents have `is_builtin=1` and may have `module_path`/`router_attr`. Remote agents (`neuraltrader`, `volatilityhunter`) have `is_builtin=0` and require `remote_url` + `secret_env`.

2. **Outbound contracts.** `AgentConnector` selects a profile per agent ID. Do not break the existing signature schemes:
   - `neuraltrader`: `METHOD\nPATH\nTIMESTAMP\nSHA256(body)`
   - `volatilityhunter`: `timestamp|METHOD|path|bodyhash` (empty body = empty hash string)
   - default: `timestamp:path:body`

3. **Built-in snapshots.** For built-in agents, `AgentConnector` returns a local snapshot instead of making HTTP calls. Keep this fallback so local tests and local mode work without remote URLs.

4. **Commands.** Commands are audited in `ExternalCommandAudit`. Reject commands for agents whose profile has `command_path=None` (e.g. VolatilityHunter is read-only).

5. **Webhooks.** Inbound webhooks are HMAC-verified with replay protection via `WebhookReceipt`. Do not weaken TTL or nonce checks.

6. **Test before finishing.** Run the control-plane tests:
   ```powershell
   python -m pytest tests/test_control_plane.py -v
   ```
