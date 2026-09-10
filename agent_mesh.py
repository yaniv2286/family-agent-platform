"""Agent Mesh: intra-platform message bus for built-in agents.

Provides two patterns:
  1. Publish/subscribe (fire-and-forget) events, backed by event_bus.py.
  2. Request/response (ask) between agents, with an audit trail in the
     same AgentEvent / AgentAction tables.

Both patterns enforce correlation_id, causation_id and a depth cap so
multi-agent chains cannot recurse forever.
"""
import asyncio
import json
import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Optional

from database import AgentAction, AgentEvent, SessionLocal
from event_bus import EventBus, event_bus


AskHandler = Callable[[AgentEvent, dict], Awaitable[dict]]
EventHandler = Callable[[AgentEvent, dict], Awaitable[None]]


class AgentMesh:
    """Central router for built-in agent collaboration.

    Remote agents remain the responsibility of agent_connector.py / the
    control plane.  The mesh only routes to in-process handlers; a caller
    that needs a remote agent should use AgentConnector directly or build
    an in-process adapter that calls AgentConnector inside an ask handler.
    """

    DEFAULT_ASK_TIMEOUT = 10.0
    MAX_DEPTH = 5

    def __init__(
        self,
        session_factory=SessionLocal,
        event_bus: EventBus = event_bus,
    ):
        self.session_factory = session_factory
        self.event_bus = event_bus
        # ask handlers keyed by (agent_id, intent)
        self._ask_handlers: Dict[tuple[str, str], AskHandler] = {}
        # pending ask futures keyed by correlation_id
        self._pending: Dict[str, asyncio.Future] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------
    def on_event(self, agent_id: str, event_type: str, handler: EventHandler) -> EventHandler:
        """Register a fire-and-forget event handler for this agent."""
        handler.agent_name = agent_id  # type: ignore[attr-defined]
        self.event_bus.registry.register(event_type, handler)
        return handler

    def on_ask(self, agent_id: str, intent: str, handler: AskHandler) -> AskHandler:
        """Register a request/response handler for (agent_id, intent)."""
        self._ask_handlers[(agent_id, intent)] = handler
        return handler

    def handler_for(self, agent_id: str, intent: str) -> Optional[AskHandler]:
        return self._ask_handlers.get((agent_id, intent))

    # ------------------------------------------------------------------
    # Fire-and-forget publish (delegated to event bus)
    # ------------------------------------------------------------------
    async def publish(
        self,
        event_type: str,
        user_id: int,
        child_id: int,
        source_agent: str,
        payload: Optional[dict] = None,
        correlation_id: Optional[str] = None,
        causation_id: Optional[int] = None,
        depth: int = 0,
    ) -> AgentEvent:
        return await self.event_bus.publish(
            event_type=event_type,
            user_id=user_id,
            child_id=child_id,
            source_agent=source_agent,
            payload=payload,
            correlation_id=correlation_id,
            causation_id=causation_id,
            depth=depth,
        )

    # ------------------------------------------------------------------
    # Request / response
    # ------------------------------------------------------------------
    async def ask(
        self,
        to_agent: str,
        intent: str,
        user_id: int,
        child_id: int,
        source_agent: str,
        payload: Optional[dict] = None,
        correlation_id: Optional[str] = None,
        causation_id: Optional[int] = None,
        depth: int = 0,
        timeout: Optional[float] = None,
    ) -> dict:
        """Ask another built-in agent a question and wait for an answer.

        The audit trail is written to AgentEvent / AgentAction exactly like
        pub/sub events so the collaboration graph remains inspectable.
        """
        if depth > self.MAX_DEPTH:
            return {
                "ok": False,
                "error": f"ask depth exceeded maximum {self.MAX_DEPTH}",
            }

        handler = self.handler_for(to_agent, intent)
        if handler is None:
            return {
                "ok": False,
                "error": f"no ask handler for agent={to_agent} intent={intent}",
            }

        corr = correlation_id or str(uuid.uuid4())
        event = await self._persist_ask_event(
            to_agent=to_agent,
            intent=intent,
            user_id=user_id,
            child_id=child_id,
            source_agent=source_agent,
            payload=payload,
            correlation_id=corr,
            causation_id=causation_id,
            depth=depth,
        )

        action = await self._persist_ask_action(
            event_id=event.id,
            agent_name=to_agent,
            intent=intent,
            user_id=user_id,
            child_id=child_id,
        )

        try:
            result = await asyncio.wait_for(
                handler(event, payload or {}),
                timeout=timeout or self.DEFAULT_ASK_TIMEOUT,
            )
            action.status = "completed"
            action.completed_at = datetime.utcnow()
            action.detail_json = json.dumps({"result": result}, ensure_ascii=False)
            event.status = "processed"
            event.processed_at = datetime.utcnow()
            return {"ok": True, "result": result, "correlation_id": corr}
        except asyncio.TimeoutError as exc:
            action.status = "failed"
            action.completed_at = datetime.utcnow()
            action.detail_json = json.dumps({"error": "ask timeout"}, ensure_ascii=False)
            event.status = "failed"
            event.error = "ask timeout"
            event.processed_at = datetime.utcnow()
            raise AgentMeshTimeout(f"ask {to_agent}.{intent} timed out") from exc
        except Exception as exc:
            action.status = "failed"
            action.completed_at = datetime.utcnow()
            action.detail_json = json.dumps({"error": str(exc)}, ensure_ascii=False)
            event.status = "failed"
            event.error = str(exc)
            event.processed_at = datetime.utcnow()
            return {"ok": False, "error": str(exc), "correlation_id": corr}
        finally:
            async with self.session_factory() as db:
                db.add(action)
                db.add(event)
                await db.commit()

    async def _persist_ask_event(
        self,
        to_agent: str,
        intent: str,
        user_id: int,
        child_id: int,
        source_agent: str,
        payload: Optional[dict],
        correlation_id: str,
        causation_id: Optional[int],
        depth: int,
    ) -> AgentEvent:
        event_type = f"ask.{to_agent}.{intent}"
        event = AgentEvent(
            event_type=event_type,
            user_id=user_id,
            child_id=child_id,
            source_agent=source_agent,
            payload_json=json.dumps(payload or {}, ensure_ascii=False),
            correlation_id=correlation_id,
            causation_id=causation_id,
            depth=depth,
            status="pending",
        )
        async with self.session_factory() as db:
            db.add(event)
            await db.commit()
            await db.refresh(event)
        return event

    async def _persist_ask_action(
        self,
        event_id: int,
        agent_name: str,
        intent: str,
        user_id: int,
        child_id: int,
    ) -> AgentAction:
        action = AgentAction(
            event_id=event_id,
            user_id=user_id,
            child_id=child_id,
            agent_name=agent_name,
            action_type=f"ask.{intent}",
            status="running",
        )
        async with self.session_factory() as db:
            db.add(action)
            await db.commit()
            await db.refresh(action)
        return action


class AgentMeshTimeout(TimeoutError):
    pass


agent_mesh = AgentMesh()
