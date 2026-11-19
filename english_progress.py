"""Shared English-course progress bookkeeping: stars, wallet, and rewards.

Used both by the scripted lesson endpoints (a slide-lesson the child taps
through) and by the free-chat tutor: when KOKO reports that the course lesson
she was driving reached its summary stage, the syllabus's next lesson is
marked complete here so both paths write identical progress.
"""
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import select

from database import (
    EnglishChildWallet,
    EnglishCoursePlacement,
    EnglishLessonProgress,
    TutorHistorySessionLocal,
)
from english_curriculum import COURSES, get_course, get_lesson
from lesson_engine import available_lesson_ids, load_lesson

logger = logging.getLogger(__name__)

# A finished lesson is worth a bonus star on top of the per-slide stars, and
# finishing a whole unit is worth two more.
LESSON_BONUS_STARS = 1
UNIT_BONUS_STARS = 2


def _unit_completed(meta: dict, done_ids: set) -> bool:
    unit = next(
        (u for c in COURSES for u in c["units"] if u["unit_id"] == meta["unit_id"]),
        None,
    )
    authored = set(available_lesson_ids())
    ids = [
        l["lesson_id"]
        for l in (unit or {}).get("lessons", [])
        if l["lesson_id"] in authored
    ]
    return bool(ids) and all(l in done_ids for l in ids)


async def record_lesson_completion(
    child_name: str, lesson_id: str, stars_earned: int
) -> dict:
    """Mark a lesson done and credit the child's star bank.

    Replays keep the best star count and never double-credit the wallet.
    """
    lesson = load_lesson(lesson_id) or {}
    meta = get_lesson(lesson_id)
    stars_total = lesson.get("stars_total", 0)
    earned = max(0, min(stars_earned, stars_total))
    async with TutorHistorySessionLocal() as db:
        row = (await db.execute(
            select(EnglishLessonProgress).where(
                EnglishLessonProgress.child_name == child_name,
                EnglishLessonProgress.lesson_id == lesson_id,
            )
        )).scalar_one_or_none()
        already = row is not None and row.completed
        if row is None:
            row = EnglishLessonProgress(child_name=child_name, lesson_id=lesson_id)
            db.add(row)
        row.completed = 1
        row.stars = max(row.stars or 0, earned)
        row.updated_at = datetime.utcnow()

        wallet = await db.get(EnglishChildWallet, child_name)
        if wallet is None:
            wallet = EnglishChildWallet(child_name=child_name)
            db.add(wallet)

        stars = row.stars
        reward_earned = False
        unit_completed = False
        if not already:
            gained = earned + LESSON_BONUS_STARS
            done_ids = set((await db.execute(
                select(EnglishLessonProgress.lesson_id).where(
                    EnglishLessonProgress.child_name == child_name,
                    EnglishLessonProgress.completed == 1,
                )
            )).scalars().all()) | {lesson_id}
            if meta is not None:
                unit_completed = _unit_completed(meta, done_ids)
            if unit_completed:
                gained += UNIT_BONUS_STARS
            wallet.total_stars = (wallet.total_stars or 0) + gained
            if wallet.reward_every and len(done_ids) % wallet.reward_every == 0:
                wallet.rewards_pending = (wallet.rewards_pending or 0) + 1
                reward_earned = True
        wallet.updated_at = datetime.utcnow()
        result = {
            "lesson_id": lesson_id,
            "stars": stars,
            "total_stars": wallet.total_stars or 0,
            "unit_completed": unit_completed,
            "reward_earned": reward_earned,
            "reward_text": (wallet.reward_text or "") if reward_earned else "",
            "rewards_pending": wallet.rewards_pending or 0,
        }
        await db.commit()
    return result


async def complete_next_course_lesson(child_name: str) -> Optional[dict]:
    """Mark the syllabus's next uncompleted lesson done for a placed child.

    The free-chat tutor drives the same course lesson the catalog shows as
    "next", so when KOKO reports lesson_finished, this is the row to write.
    Returns the completion dict, or None when the child is unplaced or the
    course is already fully done.
    """
    async with TutorHistorySessionLocal() as db:
        placement = await db.get(EnglishCoursePlacement, child_name)
        if placement is None:
            return None
        course_id = placement.course_id
        done_ids = set((await db.execute(
            select(EnglishLessonProgress.lesson_id).where(
                EnglishLessonProgress.child_name == child_name,
                EnglishLessonProgress.completed == 1,
            )
        )).scalars().all())
    course = get_course(course_id)
    if course is None:
        return None
    authored = set(available_lesson_ids())
    next_id = None
    for unit in course["units"]:
        for lesson in unit["lessons"]:
            lid = lesson["lesson_id"]
            if lid in authored and lid not in done_ids:
                next_id = lid
                break
        if next_id:
            break
    if next_id is None:
        return None
    lesson = load_lesson(next_id) or {}
    return await record_lesson_completion(
        child_name, next_id, lesson.get("stars_total", 0))
