import json
from typing import List, Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from database import MemoryEvidence, MemoryFact, ParentDirective, SessionLocal, User

PRECEDENCE = {
    "safety_constraint": 500,
    "parent_directive": 400,
    "relationship": 380,
    "goal": 300,
    "behavior": 260,
    "recommendation": 200,
    "skill": 220,
    "preference": 100,
    "episode": 40,
}


class MemoryManager:
    def __init__(self, session_factory=SessionLocal):
        self.session_factory = session_factory

    async def add_fact(
        self, user_id: int, child_id: int, domain: str, category: str, key: str,
        value, confidence: float = 1.0, evidence=None, source_agent: Optional[str] = None,
        db: Optional[AsyncSession] = None,
    ) -> MemoryFact:
        fact = MemoryFact(
            user_id=user_id, child_id=child_id, domain=domain, category=category,
            key=key, value=json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value,
            confidence=max(0.0, min(float(confidence), 1.0)),
            evidence_json=json.dumps(evidence or [], ensure_ascii=False), source_agent=source_agent,
        )
        if db is not None:
            db.add(fact)
            return fact
        async with self.session_factory() as session:
            session.add(fact)
            await session.flush()
            for item in evidence or []:
                content = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
                source_ref = str(item.get("event_id")) if isinstance(item, dict) and item.get("event_id") else None
                session.add(MemoryEvidence(fact_id=fact.id, source_type="event" if source_ref else "observation",
                                           source_ref=source_ref, content=content))
            await session.commit()
            await session.refresh(fact)
            return fact

    async def get_facts_for_child(self, child_id: int, domain: Optional[str] = None) -> List[MemoryFact]:
        async with self.session_factory() as db:
            query = select(MemoryFact).where(MemoryFact.child_id == child_id, MemoryFact.active == 1)
            if domain:
                query = query.where(MemoryFact.domain == domain)
            rows = (await db.execute(query)).scalars().all()
            return sorted(rows, key=lambda row: (PRECEDENCE.get(row.category, 0), row.confidence), reverse=True)

    async def get_facts(self, user_id: int, child_id: int, domain: Optional[str] = None) -> List[MemoryFact]:
        async with self.session_factory() as db:
            query = select(MemoryFact).where(
                MemoryFact.user_id == user_id, MemoryFact.child_id == child_id, MemoryFact.active == 1
            )
            if domain:
                query = query.where(MemoryFact.domain == domain)
            rows = (await db.execute(query)).scalars().all()
            return sorted(rows, key=lambda row: (PRECEDENCE.get(row.category, 0), row.confidence), reverse=True)

    async def get_active_directives(
        self, child_name: Optional[str] = None, subject: Optional[str] = None,
        child_id: Optional[int] = None, limit: int = 20,
    ) -> List[ParentDirective]:
        async with self.session_factory() as db:
            if child_id is None and child_name:
                user = (await db.execute(select(User).where(User.name == child_name))).scalar_one_or_none()
                child_id = user.id if user else None
            query = select(ParentDirective).where(ParentDirective.status == "active")
            if child_id is not None:
                query = query.where(or_(ParentDirective.child_id == child_id, ParentDirective.child_id.is_(None)))
            elif child_name:
                query = query.where(or_(ParentDirective.child_name == child_name, ParentDirective.child_name.is_(None)))
            if subject:
                query = query.where(or_(ParentDirective.subject == subject, ParentDirective.subject.is_(None)))
            return (await db.execute(
                query.order_by(ParentDirective.priority.desc(), ParentDirective.created_at.desc()).limit(limit)
            )).scalars().all()

    async def get_context(self, child_id: int, domain: Optional[str] = None):
        facts = await self.get_facts_for_child(child_id, domain)
        directives = await self.get_active_directives(child_id=child_id, subject=domain)
        items = [
            {"kind": "fact", "category": f.category, "content": f.value,
             "priority": PRECEDENCE.get(f.category, 0), "confidence": f.confidence}
            for f in facts
        ]
        items.extend({"kind": "directive", "category": "parent_directive", "content": d.message,
                      "priority": PRECEDENCE["parent_directive"] + d.priority, "confidence": 1.0}
                     for d in directives)
        return sorted(items, key=lambda item: item["priority"], reverse=True)


memory_manager = MemoryManager()
