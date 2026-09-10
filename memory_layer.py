"""Unified user memory layer for all Koko agents.

This module wraps the existing MemoryFact store and agent history into a single
service every agent can use to load "what I know about this user" before
responding and to store insights after interacting.

Supported memory categories:
- fact        : verified facts (name, grade, equipment, constraints)
- preference  : likes/dislikes ("hates jumping jacks", "prefers short answers")
- skill       : demonstrated abilities ("mastered subtraction", "knows colors")
- behavior    : engagement patterns ("practices at 4pm", "drops off after 10 min")
- relationship: rapport/context ("likes being called champion", "shared joke")
- episode     : recent interaction snapshots
"""

import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from database import MemoryFact, MemoryEvidence, SessionLocal, User
from memory_manager import PRECEDENCE, memory_manager


class MemoryCategory:
    FACT = "fact"
    PREFERENCE = "preference"
    SKILL = "skill"
    BEHAVIOR = "behavior"
    RELATIONSHIP = "relationship"
    EPISODE = "episode"


# Merge with memory_manager precedence so the new categories sort correctly.
_MEMORY_PRECEDENCE = {
    **PRECEDENCE,
    MemoryCategory.SKILL: 220,
    MemoryCategory.BEHAVIOR: 250,
    MemoryCategory.RELATIONSHIP: 350,
    MemoryCategory.EPISODE: 40,
}


class UserMemory:
    """Read/write service for a user's cross-agent memory."""

    def __init__(self, session_factory=SessionLocal):
        self.session_factory = session_factory

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def get_context(
        self,
        user_id: int,
        agent_id: str,
        db: Optional[AsyncSession] = None,
    ) -> Dict[str, Any]:
        """Return a structured summary of what we know about `user_id`.

        The result is organized into buckets that agents can drop directly
        into their system prompts or use for routing decisions.
        """
        should_close = db is None
        if should_close:
            db = self.session_factory()

        try:
            profile = await self._get_user_profile(user_id, db)
            facts = await self._get_active_facts(user_id, db)
            episodes = [f for f in facts if f.category == MemoryCategory.EPISODE]
            non_episodes = [f for f in facts if f.category != MemoryCategory.EPISODE]

            # Recent episodes are time-bound; everything else is ranked by
            # category precedence + confidence.
            recent_episodes = sorted(
                episodes,
                key=lambda f: f.created_at or datetime.min,
                reverse=True,
            )[:10]

            ranked = sorted(
                non_episodes,
                key=lambda f: (
                    _MEMORY_PRECEDENCE.get(f.category, 0),
                    f.confidence or 0.0,
                ),
                reverse=True,
            )

            def bucket(category: str) -> List[Dict[str, Any]]:
                return [
                    {
                        "key": f.key,
                        "value": _loads(f.value),
                        "confidence": f.confidence,
                        "source": f.source_agent,
                        "since": f.created_at.isoformat() if f.created_at else None,
                    }
                    for f in ranked
                    if f.category == category
                ]

            return {
                "user_id": user_id,
                "agent_id": agent_id,
                "profile": profile,
                "summary": await self._build_summary(profile, ranked, db),
                "facts": bucket(MemoryCategory.FACT),
                "preferences": bucket(MemoryCategory.PREFERENCE),
                "skills": bucket(MemoryCategory.SKILL),
                "behaviors": bucket(MemoryCategory.BEHAVIOR),
                "relationship": bucket(MemoryCategory.RELATIONSHIP),
                "recent_episodes": [
                    {
                        "key": f.key,
                        "value": _loads(f.value),
                        "when": f.created_at.isoformat() if f.created_at else None,
                    }
                    for f in recent_episodes
                ],
            }
        finally:
            if should_close:
                await db.close()

    async def store_insights(
        self,
        user_id: int,
        agent_id: str,
        insights: List[Dict[str, Any]],
        db: Optional[AsyncSession] = None,
    ) -> List[MemoryFact]:
        """Store a list of extracted insights as memory facts.

        Each insight dict must contain:
          - category: one of MemoryCategory values
          - key: short stable identifier
          - value: string or JSON-serializable object
          - confidence: float 0.0-1.0 (optional, default 0.8)
          - evidence: optional list of evidence strings/dicts
        """
        if not insights:
            return []

        should_close = db is None
        if should_close:
            db = self.session_factory()

        created = []
        try:
            for insight in insights:
                category = insight.get("category", MemoryCategory.PREFERENCE)
                key = insight.get("key", "unknown")
                value = insight.get("value", "")
                confidence = float(insight.get("confidence", 0.8))
                evidence = insight.get("evidence") or []

                # Deactivate an older fact with the same (category, key, source)
                # so we keep the user's memory compact and current.
                await self._deactivate_similar(user_id, agent_id, category, key, db)

                fact = await memory_manager.add_fact(
                    user_id=user_id,
                    child_id=user_id,
                    domain=_agent_domain(agent_id),
                    category=category,
                    key=key,
                    value=value,
                    confidence=max(0.0, min(1.0, confidence)),
                    evidence=evidence,
                    source_agent=agent_id,
                    db=db,
                )
                created.append(fact)

            if should_close:
                await db.commit()
                for fact in created:
                    await db.refresh(fact)
            else:
                await db.flush()

            logger.info(
                "Stored insights",
                user_id=user_id,
                agent_id=agent_id,
                count=len(created),
            )
            return created
        except Exception as exc:
            logger.warning(
                "Failed to store insights",
                user_id=user_id,
                agent_id=agent_id,
                error=str(exc),
            )
            if should_close:
                await db.rollback()
            raise
        finally:
            if should_close:
                await db.close()

    async def add_episode(
        self,
        user_id: int,
        agent_id: str,
        episode_type: str,
        content: Any,
        metadata: Optional[Dict[str, Any]] = None,
        db: Optional[AsyncSession] = None,
    ) -> MemoryFact:
        """Record a recent interaction episode for context retrieval."""
        value = {
            "type": episode_type,
            "content": content,
            "metadata": metadata or {},
        }
        # Keep episodes compact; rotate out anything older than 30 days.
        await self._rotate_episodes(user_id, agent_id, db)
        facts = await self.store_insights(
            user_id=user_id,
            agent_id=agent_id,
            insights=[
                {
                    "category": MemoryCategory.EPISODE,
                    "key": f"{episode_type}_{datetime.utcnow().isoformat()}",
                    "value": value,
                    "confidence": 1.0,
                }
            ],
            db=db,
        )
        return facts[0]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _get_user_profile(
        self, user_id: int, db: AsyncSession
    ) -> Optional[Dict[str, Any]]:
        user = await db.get(User, user_id)
        if not user:
            return None
        return {
            "id": user.id,
            "name": user.name,
            "role": user.role,
            "grade_level": user.grade_level,
            "interests": user.interests,
        }

    async def _get_active_facts(
        self, user_id: int, db: AsyncSession
    ) -> List[MemoryFact]:
        rows = await db.execute(
            select(MemoryFact).where(
                MemoryFact.child_id == user_id, MemoryFact.active == 1
            )
        )
        return rows.scalars().all()

    async def _build_summary(
        self,
        profile: Optional[Dict[str, Any]],
        facts: List[MemoryFact],
        db: AsyncSession,
    ) -> str:
        """Build a one-paragraph human-readable summary for prompts."""
        if not profile:
            return "No user profile found."

        lines = [
            f"Student: {profile['name']} (grade: {profile.get('grade_level') or 'unknown'}).",
        ]
        if profile.get("interests"):
            lines.append(f"Known interests: {profile['interests']}.")

        # Top facts/preferences/skills per category.
        for category in (
            MemoryCategory.SKILL,
            MemoryCategory.PREFERENCE,
            MemoryCategory.BEHAVIOR,
            MemoryCategory.RELATIONSHIP,
            MemoryCategory.FACT,
        ):
            items = [f for f in facts if f.category == category]
            if items:
                label = category.capitalize()
                snippets = ", ".join(
                    f"{_loads(f.value)}" for f in items[:5]
                )
                lines.append(f"{label}: {snippets}.")

        return " ".join(lines)

    async def _deactivate_similar(
        self,
        user_id: int,
        agent_id: str,
        category: str,
        key: str,
        db: AsyncSession,
    ) -> None:
        """Mark older facts with the same key/category/source as inactive."""
        await db.execute(
            text(
                "UPDATE memory_facts SET active = 0, updated_at = :now WHERE "
                "child_id = :user_id AND category = :category AND key = :key "
                "AND source_agent = :source AND active = 1"
            ),
            {
                "now": datetime.utcnow(),
                "user_id": user_id,
                "category": category,
                "key": key,
                "source": agent_id,
            },
        )

    async def _rotate_episodes(
        self,
        user_id: int,
        agent_id: str,
        db: Optional[AsyncSession] = None,
    ) -> None:
        """Deactivate episodes older than 30 days to keep context fresh."""
        cutoff = datetime.utcnow() - timedelta(days=30)
        should_close = db is None
        if should_close:
            db = self.session_factory()
        try:
            await db.execute(
                text(
                    "UPDATE memory_facts SET active = 0, updated_at = :now WHERE "
                    "child_id = :user_id AND category = :category AND created_at < :cutoff AND active = 1"
                ),
                {
                    "now": datetime.utcnow(),
                    "user_id": user_id,
                    "category": MemoryCategory.EPISODE,
                    "cutoff": cutoff,
                },
            )
            if should_close:
                await db.commit()
        finally:
            if should_close:
                await db.close()


# Global singleton, matching the memory_manager pattern.
user_memory = UserMemory()


# ------------------------------------------------------------------
# Private utilities
# ------------------------------------------------------------------
def _loads(value: str) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, ValueError):
        return value or ""


def _agent_domain(agent_id: str) -> str:
    """Map an agent identifier to the legacy memory domain."""
    if "math" in agent_id.lower():
        return "math"
    if "english" in agent_id.lower():
        return "english"
    if "fitness" in agent_id.lower() or "coach" in agent_id.lower():
        return "fitness"
    if "nutrition" in agent_id.lower():
        return "nutrition"
    return agent_id.lower()
