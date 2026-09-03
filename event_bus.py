import json
import uuid
from datetime import datetime

from database import AgentAction, AgentEvent, SessionLocal
from agent_registry import agent_registry


class EventBus:
    MAX_DEPTH = 5

    def __init__(self, session_factory=SessionLocal, registry=agent_registry):
        self.session_factory = session_factory
        self.registry = registry

    async def publish(self, event_type, user_id, child_id, source_agent, payload=None,
                      correlation_id=None, causation_id=None, depth=0):
        async with self.session_factory() as db:
            event = AgentEvent(
                event_type=event_type, user_id=user_id, child_id=child_id,
                source_agent=source_agent, payload_json=json.dumps(payload or {}, ensure_ascii=False),
                correlation_id=correlation_id or str(uuid.uuid4()), causation_id=causation_id,
                depth=depth, status="blocked" if depth > self.MAX_DEPTH else "pending",
            )
            db.add(event)
            await db.commit()
            await db.refresh(event)

        if depth > self.MAX_DEPTH:
            return event

        failures = []
        for handler in self.registry.handlers_for(event_type):
            agent_name = getattr(handler, "agent_name", handler.__module__)
            async with self.session_factory() as db:
                action = AgentAction(event_id=event.id, user_id=user_id, child_id=child_id,
                                     agent_name=agent_name, action_type=event_type, status="running")
                db.add(action)
                await db.commit()
                await db.refresh(action)
                try:
                    await handler(event, payload or {})
                    action.status = "completed"
                    action.completed_at = datetime.utcnow()
                except Exception as exc:
                    action.status = "failed"
                    action.detail_json = json.dumps({"error": str(exc)})
                    action.completed_at = datetime.utcnow()
                    failures.append(str(exc))
                await db.commit()

        async with self.session_factory() as db:
            persisted = await db.get(AgentEvent, event.id)
            persisted.status = "failed" if failures else "processed"
            persisted.error = "; ".join(failures) or None
            persisted.processed_at = datetime.utcnow()
            await db.commit()
            await db.refresh(persisted)
            return persisted


event_bus = EventBus()
