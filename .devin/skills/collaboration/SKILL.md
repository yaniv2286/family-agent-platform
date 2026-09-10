---
name: collaboration
description: Work on the agent collaboration layer - event bus, agent mesh, memory layer, memory manager, and the collaboration inspection API.
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

You are the Collaboration & Memory specialist for the Koko platform.

Scope: `agent_mesh.py`, `event_bus.py`, `memory_layer.py`, `memory_manager.py`, `collaboration_api.py`, `policy_engine.py`, and the `/api/collaboration/*` endpoints.

When working on this skill:

1. **Event bus.** `event_bus.publish()` writes `AgentEvent` rows and dispatches to registered handlers. Handlers are async and should not block the request path. Use `agent_registry.register(event_type, handler)` or `agent_mesh.on_event()`.

2. **Agent mesh ask pattern.** `agent_mesh.ask(to_agent, intent, ...)` is request/response with a depth cap (`MAX_DEPTH=5`) and correlation/causation IDs. Always check `handler_for()` first; return a clear error if no handler is registered.

3. **Memory categories.** `MemoryCategory` defines `fact`, `preference`, `skill`, `behavior`, `relationship`, `episode`. Precedence is merged from `memory_manager.PRECEDENCE`; keep category weights stable so cross-agent context ranks correctly.

4. **Parent directives.** Directives have `status`, `priority`, `scope`, and `subject`. The active set is returned by `memory_manager.get_active_directives()`. Lifecycle updates via `/api/collaboration/directives/{directive_id}` only allow `completed` or `cancelled`.

5. **Audit inspection.** `/api/collaboration/events/{child_id}` and `/api/collaboration/actions/{child_id}` expose the collaboration graph. They return JSON-decoded payloads/details.

6. **Correlations.** Always populate `correlation_id`, `causation_id`, and `depth` on mesh ask/events so the collaboration graph remains traceable.

7. **Test before finishing.** Run the collaboration and memory tests:
   ```powershell
   python -m pytest tests/test_collaboration.py tests/test_memory_layer.py -v
   ```
