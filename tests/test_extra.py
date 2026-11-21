import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

import json

from tutors import (
    MathTutor,
    _assess_english_speech,
    _parse_gesture,
    _parse_json_reply,
    _parse_learned_interests,
)
from database import (
    ChatHistory,
    StudentProfile,
    TutorHistorySessionLocal,
    get_latest_parent_feedback,
    tutor_history_engine,
)

APP_PIN = "test-pin-1234"


class TestCreativePedagogy:
    def test_math_prompt_requires_testing_child_created_methods(self):
        prompt = MathTutor().generate_system_prompt(
            {"name": "נויה", "grade_level": "כיתה ה", "interests": None}
        )
        assert "אל תדחה/י אותה" in prompt
        assert "525 פחות 280" in prompt
        assert "יותר מדרך אחת" in prompt

    def test_truncated_json_never_reaches_child_as_raw_json(self):
        raw = '{"reply_text": "בואי ננסה דרך קלה יותר:\n1. נוסיף 2 לשני המספרים.\n2. נחשב 525 פחות 280'
        reply, points, skill_id, correct, hints = _parse_json_reply(raw)
        assert not reply.startswith('{')
        assert "דרך קלה יותר" in reply
        assert "525 פחות 280" in reply
        assert points == 1
        assert skill_id is None
        assert correct is None
        assert hints == 0


class TestAvatarGesture:
    """The tutor's JSON "gesture" field drives the on-screen avatar. Parsing
    must be whitelist-strict and never break the reply itself."""

    def test_valid_gesture_extracted(self):
        raw = '{"reply_text": "בוא נחלק את המילה!", "points_earned": 1, "gesture": "clap"}'
        assert _parse_gesture(raw) == "clap"

    def test_gesture_case_and_whitespace_normalized(self):
        raw = '{"reply_text": "שלום", "gesture": "  Wave "}'
        assert _parse_gesture(raw) == "wave"

    def test_unknown_gesture_rejected(self):
        raw = '{"reply_text": "שלום", "gesture": "backflip"}'
        assert _parse_gesture(raw) is None

    def test_missing_or_malformed_gesture(self):
        assert _parse_gesture('{"reply_text": "שלום"}') is None
        assert _parse_gesture("סתם טקסט בלי ג'ייסון") is None
        assert _parse_gesture("") is None
        assert _parse_gesture(None) is None

    def test_english_prompt_offers_gesture_field(self):
        from tutors import EnglishTutor
        prompt = EnglishTutor().generate_system_prompt(
            {"name": "נויה", "grade_level": "כיתה ג", "interests": None}
        )
        assert '"gesture"' in prompt
        assert "clap" in prompt and "celebrate" in prompt


class TestLearnedInterests:
    """KOK records new interests the child states mid-lesson, so the next
    lesson opens already knowing them."""

    def test_parse_extracts_items(self):
        raw = ('{"reply_text": "כל הכבוד!", "points_earned": 2, '
               '"learned_interests": ["ריקודים", "dinosaurs", " "]}')
        assert _parse_learned_interests(raw) == ["ריקודים", "dinosaurs"]

    def test_parse_tolerates_missing_or_malformed(self):
        assert _parse_learned_interests('{"reply_text": "היי"}') == []
        assert _parse_learned_interests("סתם טקסט") == []
        assert _parse_learned_interests("") == []
        assert _parse_learned_interests(
            '{"learned_interests": "ריקודים"}') == []

    async def test_english_turn_merges_interests_into_profile(
        self, client, child_user, mock_openai
    ):
        mock_openai.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(
                content=(
                    '{"reply_text": "את אוהבת לרקוד? נלמד dance!", '
                    '"points_earned": 1, "skill_id": "english.vocabulary.basic", '
                    '"learned_interests": ["ריקודים", "חתולים", "football"]}'
                )
            ))],
            usage=MagicMock(model_dump=lambda: {}),
        )
        response = await client.post(
            "/api/tutor/english",
            json={
                "user_id": child_user.id,
                "messages": [{"role": "user", "content": "אני אוהבת לרקוד וחתולים"}],
                "idempotency_key": str(uuid.uuid4()),
            },
            headers={"x-app-pin": APP_PIN},
        )
        assert response.status_code == 200

        users = (await client.get("/api/users", headers={"x-app-pin": APP_PIN})).json()
        stored = next(u for u in users if u["id"] == child_user.id)["interests"]
        # The fixture interest is kept; new ones are appended; "football" is
        # already known and must not be duplicated.
        assert stored.count("football") == 1
        assert "ריקודים" in stored and "חתולים" in stored

    def test_english_prompt_documents_learned_interests(self):
        from tutors import EnglishTutor
        prompt = EnglishTutor().generate_system_prompt(
            {"name": "נויה", "grade_level": "כיתה ה", "interests": "ריקודים"}
        )
        assert "learned_interests" in prompt


class TestLessonContent:
    """Every authored lesson file must load, grade, match its catalog entry,
    and never leak the answer key to the browser."""

    def test_all_authored_lessons_are_valid(self):
        from lesson_engine import available_lesson_ids, grade, load_lesson, public_slide
        from english_curriculum import COURSES

        authored = set(available_lesson_ids())
        catalog_ids = {
            lesson["lesson_id"]
            for course in COURSES for unit in course["units"]
            for lesson in unit["lessons"]
        }
        # Every authored file is in the catalog, and every catalog lesson is authored.
        assert authored == catalog_ids
        assert len(authored) >= 19

        for lesson_id in sorted(authored):
            lesson = load_lesson(lesson_id)
            assert lesson["stars_total"] == len(lesson["slides"]), lesson_id
            assert lesson.get("intro_he"), lesson_id
            for slide in lesson["slides"]:
                assert slide["type"] in {"speak", "choice", "listen", "true_false", "order"}
                for field in ("id", "prompt_en", "prompt_he", "say", "praise_he", "hint_he"):
                    assert slide.get(field), f"{lesson_id}/{slide['id']} missing {field}"
                kind = slide["type"]
                if kind == "speak":
                    assert slide["accept"], f"{lesson_id}/{slide['id']}"
                    assert grade(slide, slide["accept"][0])["correct"]
                elif kind in ("choice", "listen"):
                    option_ids = {o["id"] for o in slide["options"]}
                    assert slide["correct"] in option_ids, f"{lesson_id}/{slide['id']}"
                    assert grade(slide, slide["correct"])["correct"]
                elif kind == "true_false":
                    assert grade(slide, str(bool(slide["correct"])).lower())["correct"]
                else:
                    assert grade(slide, slide["expected_order"])["correct"]
                # The answer key must never reach the browser payload.
                pub = public_slide(slide)
                assert "correct" not in pub and "accept" not in pub
                assert "expected_order" not in pub


class TestEnglishCourses:
    """The parent assigns a course; age and grade never do. Any child may be
    placed in any course, including Pre-A."""

    async def test_catalog_lists_every_course(self, client):
        response = await client.get("/api/tutor/english/courses", headers={"x-app-pin": APP_PIN})
        assert response.status_code == 200
        courses = {c["course_id"]: c for c in response.json()["courses"]}
        assert set(courses) == {"pre_a", "a1", "a2"}
        # Pre-A teaches letters in phonics order, anchored to Hebrew.
        pre_a = courses["pre_a"]
        titles = [l["title_he"] for u in pre_a["units"] for l in u["lessons"]]
        assert titles[0] == "לפני שנתחיל"
        assert any("T" in l["title_he"] or "טומי" in l["title_he"] for l in
                   [lesson for u in pre_a["units"] for lesson in u["lessons"]])

    async def test_unassigned_until_a_parent_picks(self, client, child_user):
        response = await client.get(
            f"/api/tutor/english/placement/{child_user.id}", headers={"x-app-pin": APP_PIN})
        assert response.status_code == 200
        assert response.json()["course"] is None

    async def test_parent_can_place_any_child_in_pre_a(self, client, child_user):
        response = await client.put(
            f"/api/tutor/english/placement/{child_user.id}",
            json={"course_id": "pre_a"},
            headers={"x-app-pin": APP_PIN},
        )
        assert response.status_code == 200
        assert response.json()["course_id"] == "pre_a"
        assert response.json()["daily_lesson_limit"] == 2

        again = await client.get(
            f"/api/tutor/english/placement/{child_user.id}", headers={"x-app-pin": APP_PIN})
        assert again.json()["course"]["cefr"] == "Pre-A"

    async def test_child_pin_cannot_change_placement(self, client, child_user, monkeypatch):
        monkeypatch.setenv("CHILD_PIN", "child-pin-9999")
        response = await client.put(
            f"/api/tutor/english/placement/{child_user.id}",
            json={"course_id": "a2"},
            headers={"x-app-pin": "child-pin-9999"},
        )
        assert response.status_code == 403

    async def test_unknown_course_rejected(self, client, child_user):
        response = await client.put(
            f"/api/tutor/english/placement/{child_user.id}",
            json={"course_id": "c2"},
            headers={"x-app-pin": APP_PIN},
        )
        assert response.status_code == 422


class TestLessonEngine:
    """A scripted lesson grades itself. The browser must never receive the
    answer key, and a child cannot blow past the daily lesson limit."""

    async def test_answer_key_never_reaches_the_browser(self, client, child_user):
        response = await client.get(
            f"/api/tutor/english/lesson/a1.gtky.01_meet_you?user_id={child_user.id}",
            headers={"x-app-pin": APP_PIN},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["slides"]
        raw = json.dumps(body)
        assert '"correct"' not in raw
        assert '"accept"' not in raw

    async def test_spoken_answer_accepts_a_child_accent(self, client, child_user):
        response = await client.post(
            f"/api/tutor/english/lesson/pre_a.letters.00_hello/answer?user_id={child_user.id}",
            json={"slide_id": "hello", "answer": "HELOO!!"},
            headers={"x-app-pin": APP_PIN},
        )
        assert response.status_code == 200
        assert response.json()["correct"] is True

    async def test_wrong_choice_is_not_marked_correct(self, client, child_user):
        response = await client.post(
            f"/api/tutor/english/lesson/a1.gtky.01_meet_you/answer?user_id={child_user.id}",
            json={"slide_id": "feelings", "answer": "angry"},
            headers={"x-app-pin": APP_PIN},
        )
        assert response.json()["correct"] is False
        assert response.json()["reveal"] == "happy"

    async def test_word_order(self, client, child_user):
        url = f"/api/tutor/english/lesson/a2.gtky.01_about_me/answer?user_id={child_user.id}"
        wrong = await client.post(url, json={"slide_id": "order", "answer": ["is", "Leo", "my", "brother"]},
                                  headers={"x-app-pin": APP_PIN})
        assert wrong.json()["correct"] is False
        right = await client.post(url, json={"slide_id": "order", "answer": ["Leo", "is", "my", "brother"]},
                                  headers={"x-app-pin": APP_PIN})
        assert right.json()["correct"] is True

    async def test_daily_lesson_limit(self, client, child_user):
        headers = {"x-app-pin": APP_PIN}
        for lesson_id in ("pre_a.letters.00_hello", "a1.gtky.01_meet_you"):
            done = await client.post(
                f"/api/tutor/english/lesson/{lesson_id}/complete?user_id={child_user.id}",
                headers=headers)
            assert done.status_code == 200, done.text
        third = await client.post(
            f"/api/tutor/english/lesson/a2.gtky.01_about_me/complete?user_id={child_user.id}",
            headers=headers)
        assert third.status_code == 409

    async def test_lesson_payload_marks_replay_vs_new(self, client, child_user):
        """`already_completed` lets the client exempt replays from the daily
        limit - a completed lesson may be re-opened even when the limit is
        reached (frontend showed a dead-looking button before this flag)."""
        headers = {"x-app-pin": APP_PIN}
        done = await client.post(
            f"/api/tutor/english/lesson/pre_a.letters.00_hello/complete?user_id={child_user.id}",
            headers=headers)
        assert done.status_code == 200, done.text
        replay = await client.get(
            f"/api/tutor/english/lesson/pre_a.letters.00_hello?user_id={child_user.id}",
            headers=headers)
        assert replay.status_code == 200
        assert replay.json()["already_completed"] is True
        fresh = await client.get(
            f"/api/tutor/english/lesson/a1.gtky.01_meet_you?user_id={child_user.id}",
            headers=headers)
        assert fresh.status_code == 200
        assert fresh.json()["already_completed"] is False

    async def test_unbuilt_lesson_is_not_startable(self, client, child_user):
        response = await client.get(
            f"/api/tutor/english/lesson/pre_a.letters.99_future?user_id={child_user.id}",
            headers={"x-app-pin": APP_PIN},
        )
        assert response.status_code == 404

    async def test_lesson_locked_outside_assigned_course(self, client, child_user):
        """Once the parent places a child, lessons in other courses are locked.
        Unplaced children can still preview any lesson."""
        headers = {"x-app-pin": APP_PIN}
        # Unplaced: any lesson is startable.
        open_lesson = await client.get(
            f"/api/tutor/english/lesson/a1.gtky.01_meet_you?user_id={child_user.id}",
            headers=headers)
        assert open_lesson.status_code == 200

        placed = await client.put(
            f"/api/tutor/english/placement/{child_user.id}",
            json={"course_id": "pre_a"}, headers=headers)
        assert placed.status_code == 200

        locked = await client.get(
            f"/api/tutor/english/lesson/a1.gtky.01_meet_you?user_id={child_user.id}",
            headers=headers)
        assert locked.status_code == 403
        # Answers are locked too, not just the slide list.
        locked_answer = await client.post(
            f"/api/tutor/english/lesson/a1.gtky.01_meet_you/answer?user_id={child_user.id}",
            json={"slide_id": "name", "answer": "my name is test"},
            headers=headers)
        assert locked_answer.status_code == 403

        own = await client.get(
            f"/api/tutor/english/lesson/pre_a.letters.00_hello?user_id={child_user.id}",
            headers=headers)
        assert own.status_code == 200

    async def test_placement_reports_lesson_progress(self, client, child_user):
        headers = {"x-app-pin": APP_PIN}
        done = await client.post(
            f"/api/tutor/english/lesson/pre_a.letters.00_hello/complete?user_id={child_user.id}",
            headers=headers)
        assert done.status_code == 200
        placement = await client.get(
            f"/api/tutor/english/placement/{child_user.id}", headers=headers)
        assert placement.json()["lesson_progress"]["pre_a.letters.00_hello"] == 4


class TestEnglishWallet:
    """The star bank: per-slide stars count what the child actually earned,
    finishing a lesson adds a bonus star, a whole unit adds two more, and the
    parent's reward cycle produces claimable gifts."""

    async def test_earned_stars_and_lesson_bonus(self, client, child_user):
        headers = {"x-app-pin": APP_PIN}
        done = await client.post(
            f"/api/tutor/english/lesson/pre_a.letters.00_hello/complete?user_id={child_user.id}",
            json={"stars_earned": 2}, headers=headers)
        assert done.status_code == 200, done.text
        body = done.json()
        # 2 slide stars stored, 2 + 1 lesson bonus banked.
        assert body["stars"] == 2
        assert body["total_stars"] == 3
        assert body["reward_earned"] is False

        placement = await client.get(
            f"/api/tutor/english/placement/{child_user.id}", headers=headers)
        assert placement.json()["wallet"]["total_stars"] == 3
        assert placement.json()["wallet"]["lessons_done"] == 1

    async def test_replay_does_not_double_count_stars(self, client, child_user):
        headers = {"x-app-pin": APP_PIN}
        url = f"/api/tutor/english/lesson/pre_a.letters.00_hello/complete?user_id={child_user.id}"
        first = await client.post(url, json={"stars_earned": 3}, headers=headers)
        again = await client.post(url, json={"stars_earned": 4}, headers=headers)
        assert again.json()["total_stars"] == first.json()["total_stars"]
        # A better replay still updates the best star count for the lesson.
        assert again.json()["stars"] == 4

    async def test_parent_reward_cycle_and_claim(self, client, child_user):
        headers = {"x-app-pin": APP_PIN}
        placed = await client.put(
            f"/api/tutor/english/placement/{child_user.id}",
            json={"course_id": "pre_a", "daily_lesson_limit": 4,
                  "reward_every": 2, "reward_text": "גלידה"},
            headers=headers)
        assert placed.status_code == 200, placed.text

        first = await client.post(
            f"/api/tutor/english/lesson/pre_a.letters.00_hello/complete?user_id={child_user.id}",
            json={"stars_earned": 4}, headers=headers)
        assert first.json()["reward_earned"] is False

        second = await client.post(
            f"/api/tutor/english/lesson/pre_a.letters.01_backyard/complete?user_id={child_user.id}",
            json={"stars_earned": 5}, headers=headers)
        body = second.json()
        assert body["reward_earned"] is True
        assert body["reward_text"] == "גלידה"
        assert body["rewards_pending"] == 1

        placement = await client.get(
            f"/api/tutor/english/placement/{child_user.id}", headers=headers)
        wallet = placement.json()["wallet"]
        assert wallet["reward_every"] == 2
        assert wallet["reward_text"] == "גלידה"
        assert wallet["rewards_pending"] == 1

        claim = await client.post(
            f"/api/tutor/english/rewards/claim/{child_user.id}", headers=headers)
        assert claim.json()["claimed"] == 1
        after = await client.get(
            f"/api/tutor/english/placement/{child_user.id}", headers=headers)
        assert after.json()["wallet"]["rewards_pending"] == 0

    async def test_child_pin_cannot_claim_rewards(self, client, child_user, monkeypatch):
        monkeypatch.setenv("CHILD_PIN", "child-pin-9999")
        response = await client.post(
            f"/api/tutor/english/rewards/claim/{child_user.id}",
            headers={"x-app-pin": "child-pin-9999"})
        assert response.status_code == 403

    async def test_chat_lesson_finish_marks_course_progress(
        self, client, child_user, mock_openai
    ):
        """A free-chat lesson that reaches its summary counts like a scripted
        one: the syllabus's next lesson is recorded with its stars."""
        headers = {"x-app-pin": APP_PIN}
        placed = await client.put(
            f"/api/tutor/english/placement/{child_user.id}",
            json={"course_id": "pre_a"}, headers=headers)
        assert placed.status_code == 200
        mock_openai.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(
                content=(
                    '{"reply_text": "כל הכבוד! סיימנו את השיעור!", '
                    '"points_earned": 5, "skill_id": "english.vocabulary.basic", '
                    '"lesson_finished": true}'
                )
            ))],
            usage=MagicMock(model_dump=lambda: {}),
        )
        response = await client.post(
            "/api/tutor/english",
            json={
                "user_id": child_user.id,
                "messages": [{"role": "user", "content": "blue"}],
                "idempotency_key": str(uuid.uuid4()),
            },
            headers=headers,
        )
        assert response.status_code == 200
        placement = (await client.get(
            f"/api/tutor/english/placement/{child_user.id}", headers=headers)).json()
        # The first uncompleted authored lesson in pre_a was marked.
        assert placement["lesson_progress"]["pre_a.letters.00_hello"] > 0
        assert placement["wallet"]["total_stars"] > 0
        assert placement["wallet"]["lessons_done"] == 1

    async def test_chat_lesson_finish_needs_placement(
        self, client, child_user, mock_openai
    ):
        """Without a parent placement there is no syllabus to mark against."""
        headers = {"x-app-pin": APP_PIN}
        mock_openai.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(
                content=(
                    '{"reply_text": "סיימנו!", "points_earned": 1, '
                    '"skill_id": "english.vocabulary.basic", '
                    '"lesson_finished": true}'
                )
            ))],
            usage=MagicMock(model_dump=lambda: {}),
        )
        response = await client.post(
            "/api/tutor/english",
            json={
                "user_id": child_user.id,
                "messages": [{"role": "user", "content": "blue"}],
                "idempotency_key": str(uuid.uuid4()),
            },
            headers=headers,
        )
        assert response.status_code == 200
        placement = (await client.get(
            f"/api/tutor/english/placement/{child_user.id}", headers=headers)).json()
        assert placement["lesson_progress"] == {}

    def test_english_prompt_documents_lesson_finished(self):
        from tutors import EnglishTutor
        prompt = EnglishTutor().generate_system_prompt(
            {"name": "ליבי", "grade_level": "כיתה א"}
        )
        assert "lesson_finished" in prompt

    async def test_unit_bonus_on_last_lesson(self, client, child_user):
        """Finishing every authored lesson in a unit is worth +2 bonus stars."""
        headers = {"x-app-pin": APP_PIN}
        placed = await client.put(
            f"/api/tutor/english/placement/{child_user.id}",
            json={"course_id": "a1", "daily_lesson_limit": 6},
            headers=headers)
        assert placed.status_code == 200
        unit_lessons = ["a1.gtky.00_hello", "a1.gtky.01_meet_you",
                        "a1.gtky.02_my_family", "a1.gtky.03_review"]
        totals = []
        for lesson_id in unit_lessons[:-1]:
            done = await client.post(
                f"/api/tutor/english/lesson/{lesson_id}/complete?user_id={child_user.id}",
                json={"stars_earned": 4}, headers=headers)
            assert done.status_code == 200, done.text
            assert done.json()["unit_completed"] is False
            totals.append(done.json()["total_stars"])
        last = await client.post(
            f"/api/tutor/english/lesson/{unit_lessons[-1]}/complete?user_id={child_user.id}",
            json={"stars_earned": 4}, headers=headers)
        body = last.json()
        assert body["unit_completed"] is True
        # Previous total + 4 earned + 1 lesson bonus + 2 unit bonus.
        assert body["total_stars"] == totals[-1] + 7


class TestUserExpressionEval:
    """Deterministic evaluation of expressions the child sends (e.g. homework).

    Regression for the production bug where Noya sent "(5+5:5)*5" and Koko
    computed (5+5)/5*5 = 10 — doing addition before division inside the
    parentheses. The correct value is (5+1)*5 = 30.
    """

    def setup_method(self):
        self.tutor = MathTutor.__new__(MathTutor)

    def _history(self, user_text):
        return [{"role": "user", "content": user_text}]

    def test_israeli_division_colon_inside_parens(self):
        # 5+5:5 => 5 + (5/5) = 6; 6*5 = 30. Koko answered 10 in production.
        result = self.tutor._extract_user_expression_eval(
            self._history("(5+5:5)*5")
        )
        assert result is not None
        expr, value, claim_correct = result
        assert value == "30"
        assert claim_correct is None

    def test_no_expression_returns_none(self):
        assert self.tutor._extract_user_expression_eval(
            self._history("אני לא יודעת")
        ) is None

    def test_single_number_is_not_an_expression(self):
        assert self.tutor._extract_user_expression_eval(
            self._history("21")
        ) is None

    def test_multiplication_sign_x(self):
        result = self.tutor._extract_user_expression_eval(
            self._history("כמה זה 3×4?")
        )
        assert result is not None
        assert result[1] == "12"

    def test_spoken_hebrew_expression_with_correct_claim(self):
        # Whisper transcript style: digits + operator words + "זה N".
        # Production bug: Koko said "קרוב" to 456-213=243 and re-checked twice.
        result = self.tutor._extract_user_expression_eval(
            self._history("456 פחות 213 זה 243!")
        )
        assert result is not None
        expr, value, claim_correct = result
        assert value == "243"
        assert claim_correct is True

    def test_spoken_hebrew_expression_with_wrong_claim(self):
        result = self.tutor._extract_user_expression_eval(
            self._history("456 פחות 213 זה 240")
        )
        assert result is not None
        assert result[1] == "243"
        assert result[2] is False

    def test_spoken_hebrew_expression_no_claim(self):
        result = self.tutor._extract_user_expression_eval(
            self._history("כמה זה 20 חלקי 5?")
        )
        assert result is not None
        assert result[1] == "4"
        assert result[2] is None

    def test_non_math_hebrew_text_returns_none(self):
        assert self.tutor._extract_user_expression_eval(
            self._history("איך אומרים חתול באנגלית?")
        ) is None

    def test_fully_spoken_expression_in_hebrew_words(self):
        # Whisper often produces the whole expression as words:
        # "מאה פחות חמישים זה חמישים".
        result = self.tutor._extract_user_expression_eval(
            self._history("מאה פחות חמישים זה חמישים")
        )
        assert result is not None
        assert result[1] == "50"
        assert result[2] is True

    def test_hebrew_words_claim_after_digits(self):
        result = self.tutor._extract_user_expression_eval(
            self._history("456 פחות 213 זה מאתיים ארבעים ושלוש")
        )
        assert result is not None
        assert result[1] == "243"
        assert result[2] is True


class TestCorrectArithmeticExtraction:
    """KOKO's question -> child's bare number answer must get deterministic
    ground truth so the LLM never doubts a correct answer.

    Regression for the live QA run where KOKO asked
    '15 מכוניות צעצוע ומישהו לוקח 7 מכוניות, כמה נשארו?', the child answered
    '8' (correct) - and KOKO replied 'בוא נבדוק שוב, נסה שוב'. The
    sentence-segment scan read offsets against assistant_text while the
    numbers came from a question substring, so no candidate was found and no
    fact was injected.
    """

    def setup_method(self):
        self.tutor = MathTutor.__new__(MathTutor)

    def _history(self, assistant_q, user_a):
        return [
            {"role": "assistant", "content": assistant_q},
            {"role": "user", "content": user_a},
        ]

    def test_worded_takeaway_question_inside_longer_reply(self):
        # 'מישהו לוקח N' = subtraction; 'כמה נשארו' in the same sentence.
        result = self.tutor._extract_correct_arithmetic_answer(
            self._history(
                "כל הכבוד! עכשיו, בוא ננסה חידה קטנה על מכוניות: "
                "אם יש לך 15 מכוניות צעצוע ומישהו לוקח 7 מכוניות, כמה מכוניות נשארו לך?",
                "8",
            )
        )
        assert result is not None
        assert result[1] == "8"

    def test_wrong_answer_does_not_confirm(self):
        result = self.tutor._extract_correct_arithmetic_answer(
            self._history(
                "אם יש לך 15 מכוניות צעצוע ומישהו לוקח 7 מכוניות, כמה מכוניות נשארו לך?",
                "9",
            )
        )
        assert result is None

    def test_spoken_hebrew_number_words_confirmed(self):
        # Production miss (Noya, 21.11): after struggling through 64-29 she
        # said "שלושים וחמש" - correct - and got generic encouragement, not
        # "נכון!". Whisper transcribes spoken numbers as words, so the
        # verifier must parse Hebrew number phrases too.
        result = self.tutor._extract_correct_arithmetic_answer(
            self._history(
                "מה ההפרש בין 64 ל-29?",
                "שלושים וחמש, אני כל כך התבלבלתי.",
            )
        )
        assert result is not None
        assert result[1] == "35"

    def test_spoken_wrong_hebrew_number_not_confirmed(self):
        result = self.tutor._extract_correct_arithmetic_answer(
            self._history(
                "מה ההפרש בין 64 ל-29?",
                "התשובה היא בעצם ארבעים וחמש.",
            )
        )
        assert result is None


class TestTtsSanitize:
    """Markdown formatting must never reach the TTS engine.

    Regression for the production bug where a numbered/bulleted explanation
    like "- 20 חלקי 4 זה 5" was read aloud as "פחות עשרים חלקי 4 זה 5"
    because the leading '-' was translated as the word "פחות".
    """

    def test_leading_dash_is_not_minus(self):
        from tutors import _sanitize_numbers_for_tts
        out = _sanitize_numbers_for_tts("- עשרים חלקי ארבע זה חמש")
        assert "פחות" not in out

    def test_markdown_bullet_before_number(self):
        from tutors import _sanitize_numbers_for_tts
        out = _sanitize_numbers_for_tts("1. נחלק את 32\n   - 20 חלקי 4 זה 5\n   - 12 חלקי 4 זה 3")
        assert "פחות" not in out
        assert "עשרים" in out

    def test_real_minus_between_numbers_still_works(self):
        from tutors import _sanitize_numbers_for_tts
        out = _sanitize_numbers_for_tts("כמה זה 10 - 4?")
        assert "פחות" in out

    def test_colon_division_read_as_chalki(self):
        from tutors import _sanitize_numbers_for_tts
        out = _sanitize_numbers_for_tts("32:4 = 8")
        assert "חלקי" in out

    def test_bold_markers_not_read_aloud(self):
        from tutors import _sanitize_numbers_for_tts
        out = _sanitize_numbers_for_tts("**כל הכבוד!**")
        assert "*" not in out


class TestEnglishSpeechAssessment:
    """Deterministic grading of corrupted child ASR transcripts.

    Regression for the production bug where Yanai said "saw a dog today"
    and Whisper produced 'song dog today' / 'סונג dog today' — the tutor
    then marked him wrong ~20 turns in a row.
    """

    def _history(self, assistant_text, user_text):
        return [
            {"role": "assistant", "content": assistant_text},
            {"role": "user", "content": user_text},
        ]

    def test_correct_pronunciation_despite_garbled_transcript(self):
        result = _assess_english_speech(
            self._history('נסה לומר: "I saw a dog today"', "song dog today")
        )
        assert result is not None
        expected, verdict, matched, missing = result
        assert verdict == "correct"
        assert {"saw", "dog", "today"}.issubset(set(matched))

    def test_hebrew_script_asr_artifacts_map_to_english(self):
        result = _assess_english_speech(
            self._history('נסה לומר: "I saw a dog today"', "סונג dog טודיי")
        )
        assert result is not None
        _, verdict, matched, missing = result
        assert verdict == "correct"
        assert {"saw", "dog", "today"}.issubset(set(matched))

    def test_partial_answer_isolated(self):
        result = _assess_english_speech(
            self._history('נסה לומר: "I saw a dog today"', "dog today")
        )
        assert result is not None
        _, verdict, matched, missing = result
        assert verdict == "partial"
        assert "saw" in missing

    def test_unrelated_answer_not_forced_into_verdict(self):
        result = _assess_english_speech(
            self._history('נסה לומר: "I saw a dog today"', "אני רוצה לשחק")
        )
        assert result is None

    def test_no_expected_phrase_returns_none(self):
        result = _assess_english_speech(
            self._history("כל הכבוד! בוא נמשיך", "ok")
        )
        assert result is None


class TestTTS:
    async def test_speech_requires_pin(self, client):
        response = await client.post("/api/tutor/speech", json={"text": "שלום"})
        assert response.status_code == 401

    async def test_speech_returns_mp3(self, client, mock_openai):
        response = await client.post(
            "/api/tutor/speech",
            json={"text": "שלום", "subject": "math"},
            headers={"x-app-pin": APP_PIN},
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/mpeg"
        assert response.content == b"fake-mp3-bytes"
        # math voice is "shimmer" by default
        call = mock_openai.audio.speech.create.call_args
        assert call.kwargs["voice"] == "shimmer"
        assert call.kwargs["model"] == "tts-1"


class TestEndSession:
    async def test_end_session_creates_learning_log(self, client, child_user):
        body = {
            "user_id": child_user.id,
            "subject": "math",
            "messages": [
                {"role": "user", "content": "2 plus 2"},
                {"role": "assistant", "content": "4"},
            ],
            "duration_minutes": 5,
        }
        response = await client.post(
            "/api/tutor/end-session",
            json=body,
            headers={"x-app-pin": APP_PIN},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["user_id"] == child_user.id
        assert data["subject"] == "math"
        assert data["topic"] == "addition"
        assert data["session_duration_minutes"] == 5


class TestHistory:
    async def test_history_requires_pin(self, client, child_user):
        response = await client.get(f"/api/history/{child_user.name}")
        assert response.status_code == 401

    async def test_history_returns_messages(self, client, child_user):
        now = datetime.now(timezone.utc)
        async with TutorHistorySessionLocal() as session:
            session.add(
                ChatHistory(
                    child_name=child_user.name,
                    subject="math",
                    role="user",
                    content="שלום",
                    timestamp=now,
                )
            )
            session.add(
                ChatHistory(
                    child_name=child_user.name,
                    subject="math",
                    role="assistant",
                    content="היי!",
                    timestamp=now,
                )
            )
            await session.commit()

        response = await client.get(
            f"/api/history/{child_user.name}?subject=math",
            headers={"x-app-pin": APP_PIN},
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["messages"]) == 2
        assert data["messages"][0]["role"] == "user"
        assert data["messages"][1]["role"] == "assistant"


class TestEnglish:
    async def test_english_idempotency_replays_previous_response(
        self, client, child_user, mock_openai
    ):
        key = str(uuid.uuid4())
        body = {
            "user_id": child_user.id,
            "messages": [{"role": "user", "content": "Hello"}],
            "idempotency_key": key,
        }

        first = await client.post(
            "/api/tutor/english",
            json=body,
            headers={"x-app-pin": APP_PIN},
        )
        assert first.status_code == 200
        first_data = first.json()
        assert first_data["points_earned"] == 3

        second = await client.post(
            "/api/tutor/english",
            json=body,
            headers={"x-app-pin": APP_PIN},
        )
        second_data = second.json()
        assert second_data["reply"] == first_data["reply"]
        assert second_data["points_earned"] == 0
        assert second_data["total_points"] == first_data["total_points"]


class TestTutorMastery:
    async def test_daily_practice_returns_deterministic_skill_plan(self, client, child_user):
        response = await client.get(
            f"/api/tutor/daily-practice/{child_user.id}?subject=math",
            headers={"x-app-pin": APP_PIN},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["child_id"] == child_user.id
        assert data["subject"] == "math"
        assert len(data["activities"]) == 3
        assert [item["purpose"] for item in data["activities"]] == ["review", "practice", "challenge"]
        assert all(item["skill_id"].startswith("math.") for item in data["activities"])
        assert data["curriculum_level"] == 1
        assert data["activities"][0]["level"] <= 1
        assert data["activities"][1]["level"] <= 1
        assert data["activities"][2]["level"] >= 1

    async def test_attempt_updates_mastery_and_parent_progress(self, client, child_user):
        payload = {
            "subject": "math",
            "skill_id": "math.addition.basic",
            "correct": True,
            "hints_used": 0,
            "difficulty": 1,
            "idempotency_key": str(uuid.uuid4()),
        }
        attempt = await client.post(
            f"/api/tutor/mastery/{child_user.id}/attempt",
            json=payload,
            headers={"x-app-pin": APP_PIN},
        )
        assert attempt.status_code == 200
        result = attempt.json()
        assert result["attempts"] == 1
        assert result["independent_correct"] == 1
        assert result["mastery"] > 0

        progress = await client.get(
            f"/api/tutor/progress/{child_user.id}",
            headers={"x-app-pin": APP_PIN},
        )
        assert progress.status_code == 200
        math = progress.json()["subjects"]["math"]
        skill = next(item for item in math["skills"] if item["skill_id"] == "math.addition.basic")
        assert skill["attempts"] == 1
        assert skill["independent_accuracy"] == 1.0
        assert math["practiced_skills"] == 1

    async def test_attempt_idempotency_does_not_double_count(self, client, child_user):
        key = str(uuid.uuid4())
        payload = {
            "subject": "english",
            "skill_id": "english.vocabulary.basic",
            "correct": False,
            "hints_used": 1,
            "difficulty": 1,
            "idempotency_key": key,
        }
        first = await client.post(
            f"/api/tutor/mastery/{child_user.id}/attempt",
            json=payload,
            headers={"x-app-pin": APP_PIN},
        )
        second = await client.post(
            f"/api/tutor/mastery/{child_user.id}/attempt",
            json=payload,
            headers={"x-app-pin": APP_PIN},
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["attempts"] == 1
        assert second.json()["idempotent_replay"] is True

    async def test_unknown_skill_is_rejected(self, client, child_user):
        response = await client.post(
            f"/api/tutor/mastery/{child_user.id}/attempt",
            json={
                "subject": "math",
                "skill_id": "math.unknown",
                "correct": True,
                "hints_used": 0,
                "difficulty": 1,
                "idempotency_key": str(uuid.uuid4()),
            },
            headers={"x-app-pin": APP_PIN},
        )
        assert response.status_code == 422


class TestChatValidation:
    async def test_chat_nonexistent_user(self, client, mock_openai):
        body = {
            "user_id": 999999,
            "subject": "math",
            "messages": [{"role": "user", "content": "שלום"}],
            "idempotency_key": str(uuid.uuid4()),
        }
        response = await client.post(
            "/api/tutor/chat",
            json=body,
            headers={"x-app-pin": APP_PIN},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["points_earned"] == 0
        assert data["total_points"] == 0
        assert "לא מצאתי" in data["reply"] or "not found" in data["reply"].lower()


class TestPINMiddleware:
    @pytest.mark.parametrize(
        "method, url, body",
        [
            ("GET", "/api/ping", None),
            ("GET", "/api/users", None),
            ("POST", "/api/tutor/chat", {"user_id": 1, "subject": "math", "messages": [], "idempotency_key": str(uuid.uuid4())}),
            ("GET", "/api/history/NoOne", None),
            ("POST", "/api/tutor/speech", {"text": "שלום", "subject": "math"}),
            ("POST", "/api/tutor/end-session", {"user_id": 1, "subject": "math", "messages": [], "duration_minutes": 1}),
        ],
    )
    async def test_endpoint_requires_pin(self, client, method, url, body):
        if method == "GET":
            response = await client.get(url)
        else:
            response = await client.post(url, json=body or {})
        assert response.status_code == 401

    @pytest.mark.parametrize(
        "method, url, body",
        [
            ("GET", "/api/ping", None),
            ("POST", "/api/tutor/chat", {"user_id": 1, "subject": "math", "messages": [], "idempotency_key": str(uuid.uuid4())}),
            ("GET", "/api/history/NoOne", None),
            ("POST", "/api/tutor/speech", {"text": "שלום", "subject": "math"}),
        ],
    )
    async def test_endpoint_accepts_valid_pin(self, client, method, url, body):
        if method == "GET":
            response = await client.get(url, headers={"x-app-pin": APP_PIN})
        else:
            response = await client.post(
                url,
                json=body or {},
                headers={"x-app-pin": APP_PIN},
            )
        assert response.status_code != 401


class TestLLMFallbacks:
    async def test_malformed_llm_response_returns_one_point(
        self, client, child_user, mock_openai
    ):
        mock_openai.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="this is not json"))],
            usage=MagicMock(model_dump=lambda: {}),
        )
        body = {
            "user_id": child_user.id,
            "subject": "math",
            "messages": [{"role": "user", "content": "שלום"}],
            "idempotency_key": str(uuid.uuid4()),
        }
        response = await client.post(
            "/api/tutor/chat",
            json=body,
            headers={"x-app-pin": APP_PIN},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["reply"] == "this is not json"
        assert data["points_earned"] == 1

    async def test_empty_json_object_reply_gets_friendly_fallback(
        self, client, child_user, mock_openai
    ):
        # Regression: production showed Koko bubbles containing literally "{}".
        mock_openai.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="{}"))],
            usage=MagicMock(model_dump=lambda: {}),
        )
        body = {
            "user_id": child_user.id,
            "subject": "math",
            "messages": [{"role": "user", "content": "שלום"}],
            "idempotency_key": str(uuid.uuid4()),
        }
        resp = await client.post(
            "/api/tutor/chat", json=body, headers={"x-app-pin": APP_PIN}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["reply"] != "{}"
        assert "ננסה שוב" in data["reply"]

    async def test_negative_points_gets_hard_capped_to_one(
        self, client, child_user, mock_openai
    ):
        mock_openai.chat.completions.create.return_value = MagicMock(
            choices=[
                MagicMock(
                    message=MagicMock(
                        content='{"reply_text": "ככה", "points_earned": -100}'
                    )
                )
            ],
            usage=MagicMock(model_dump=lambda: {}),
        )
        body = {
            "user_id": child_user.id,
            "subject": "math",
            "messages": [{"role": "user", "content": "שלום"}],
            "idempotency_key": str(uuid.uuid4()),
        }
        response = await client.post(
            "/api/tutor/chat",
            json=body,
            headers={"x-app-pin": APP_PIN},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["points_earned"] == 1


class TestSubjectSeparation:
    async def test_math_and_english_history_are_separate(
        self, client, child_user, mock_openai
    ):
        math_body = {
            "user_id": child_user.id,
            "subject": "math",
            "messages": [{"role": "user", "content": "1+1"}],
            "idempotency_key": str(uuid.uuid4()),
        }
        english_body = {
            "user_id": child_user.id,
            "messages": [{"role": "user", "content": "cat"}],
            "idempotency_key": str(uuid.uuid4()),
        }

        math_resp = await client.post(
            "/api/tutor/chat",
            json=math_body,
            headers={"x-app-pin": APP_PIN},
        )
        english_resp = await client.post(
            "/api/tutor/english",
            json=english_body,
            headers={"x-app-pin": APP_PIN},
        )
        assert math_resp.json()["total_points"] == 3
        assert english_resp.json()["total_points"] == 3

        math_hist = await client.get(
            f"/api/history/{child_user.name}?subject=math",
            headers={"x-app-pin": APP_PIN},
        )
        english_hist = await client.get(
            f"/api/history/{child_user.name}?subject=english",
            headers={"x-app-pin": APP_PIN},
        )
        assert len(math_hist.json()["messages"]) == 2
        assert len(english_hist.json()["messages"]) == 2


class TestChatHistoryPersistence:
    async def test_new_session_user_message_saved_with_prior_history(
        self, client, child_user, mock_openai
    ):
        # Regression: append_chat_messages used to slice the session payload by
        # the total DB row count, so once a child had accumulated history, every
        # new session message was silently dropped (production DB held only
        # assistant messages). Seed prior rows to simulate that state.
        now = datetime.now(timezone.utc)
        async with TutorHistorySessionLocal() as session:
            for i in range(5):
                session.add(
                    ChatHistory(
                        child_name=child_user.name,
                        subject="math",
                        role="assistant",
                        content=f"old reply {i}",
                        timestamp=now,
                    )
                )
            await session.commit()

        body = {
            "user_id": child_user.id,
            "subject": "math",
            "messages": [{"role": "user", "content": "הודעה חדשה מסשן חדש"}],
            "idempotency_key": str(uuid.uuid4()),
        }
        response = await client.post(
            "/api/tutor/chat", json=body, headers={"x-app-pin": APP_PIN}
        )
        assert response.status_code == 200

        hist = await client.get(
            f"/api/history/{child_user.name}?subject=math",
            headers={"x-app-pin": APP_PIN},
        )
        contents = [m["content"] for m in hist.json()["messages"]]
        assert "הודעה חדשה מסשן חדש" in contents

    async def test_second_turn_persists_both_messages(
        self, client, child_user, mock_openai
    ):
        first = await client.post(
            "/api/tutor/english",
            json={
                "user_id": child_user.id,
                "messages": [{"role": "user", "content": "first msg"}],
                "idempotency_key": str(uuid.uuid4()),
            },
            headers={"x-app-pin": APP_PIN},
        )
        assert first.status_code == 200

        second = await client.post(
            "/api/tutor/english",
            json={
                "user_id": child_user.id,
                "messages": [
                    {"role": "user", "content": "first msg"},
                    {"role": "assistant", "content": first.json()["reply"]},
                    {"role": "user", "content": "second msg"},
                ],
                "idempotency_key": str(uuid.uuid4()),
            },
            headers={"x-app-pin": APP_PIN},
        )
        assert second.status_code == 200

        hist = await client.get(
            f"/api/history/{child_user.name}?subject=english",
            headers={"x-app-pin": APP_PIN},
        )
        contents = [m["content"] for m in hist.json()["messages"]]
        assert "first msg" in contents
        assert "second msg" in contents


class TestOrchestrator:
    async def test_israel_day_is_converted_to_utc_bounds(self, monkeypatch):
        import orchestrator

        israel_time = datetime(2026, 9, 1, 21, 30, tzinfo=ZoneInfo("Asia/Jerusalem"))
        monkeypatch.setattr(orchestrator, "_local_now", lambda: israel_time)

        since, until = orchestrator._today_utc_bounds()

        assert since == datetime(2026, 8, 31, 21, 0)
        assert until.date() == datetime(2026, 9, 1).date()
        assert until.hour == 20
        assert until.minute == 59

    async def test_telegram_feedback_is_stored_per_child_and_acknowledged(
        self, monkeypatch
    ):
        from agents import messenger_agent

        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456")
        monkeypatch.setattr(
            messenger_agent,
            "_get_children",
            AsyncMock(
                return_value=[SimpleNamespace(name="נויה"), SimpleNamespace(name="ינאי")]
            ),
        )
        send_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(messenger_agent, "send_telegram_message", send_mock)

        await messenger_agent.check_telegram_feedback(
            {
                "update_id": 987654,
                "message": {
                    "chat": {"id": 123456},
                    "from": {"is_bot": False},
                    "text": "נויה - לחזק תרגילי לוח הכפל\nינאי - לתרגל אנגלית",
                },
            }
        )

        noya_directives = await get_latest_parent_feedback("נויה", "math")
        yanai_directives = await get_latest_parent_feedback("ינאי", "english")
        assert "נויה - לחזק תרגילי לוח הכפל" in noya_directives
        assert "ינאי - לתרגל אנגלית" in yanai_directives
        acknowledgement = send_mock.await_args.args[0]
        assert "ההנחיות התקבלו ונשמרו" in acknowledgement
        assert "נויה / חשבון" in acknowledgement
        assert "ינאי / אנגלית" in acknowledgement

    async def test_daily_report_lock_prevents_duplicate_telegrams(self, monkeypatch):
        import orchestrator

        # Pin "today" to a deterministic date so this test does not collide with
        # other tests that may have acquired the daily-report lock.
        fixed_now = datetime(2030, 1, 1, 21, 30, tzinfo=ZoneInfo("Asia/Jerusalem"))
        monkeypatch.setattr(orchestrator, "_local_now", lambda: fixed_now)

        # Stub the messenger so no real Telegram calls are made.
        send_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(orchestrator, "_send_via_messenger", send_mock)

        # First run should acquire the lock and "send" the report.
        result1 = await orchestrator.run_daily_orchestration()
        assert result1["skipped"] is False
        assert result1["telegram_sent"] is True
        assert send_mock.call_count == 1

        # Second run must be skipped by the daily-report lock and never send.
        result2 = await orchestrator.run_daily_orchestration()
        assert result2["skipped"] is True
        assert result2["telegram_sent"] is False
        assert send_mock.call_count == 1

    async def test_daily_report_strips_llm_fabricated_inactive_line(self, monkeypatch):
        # Regression: the LLM invented its own "לא פעל היום" line with a wrong
        # name (ליבי vs לביא) while also writing an active section for the same
        # child. The fabricated line must be stripped and the deterministic
        # inactive list appended instead.
        import orchestrator
        import tutors

        llm_summary = (
            "📅 דוח יומי - 20/10/2026\n"
            "**לביא**\n"
            "📐 שליטה בחיבור.\n"
            "💤 לא פעל היום: לביא"
        )

        async def fake_call_llm(system_prompt, conversation, max_tokens=None):
            return llm_summary

        monkeypatch.setattr(tutors, "call_llm", fake_call_llm)
        monkeypatch.setattr(orchestrator, "call_llm", fake_call_llm, raising=False)

        report_data = [
            {
                "child_name": "לביא",
                "grade_level": "כיתה א",
                "subjects": {
                    "math": {
                        "message_count": 5,
                        "average_words_per_message": 1.5,
                        "is_lazy_spam": False,
                        "profile_summary": "מתקדם יפה",
                    },
                    "english": {
                        "message_count": 0,
                        "average_words_per_message": 0.0,
                        "is_lazy_spam": False,
                        "profile_summary": "",
                    },
                },
                "wellness": {"workouts_completed": 0, "activity_minutes": 0, "recommendations": []},
            },
            {
                "child_name": "ליבי",
                "grade_level": "גן",
                "subjects": {
                    "math": {
                        "message_count": 0,
                        "average_words_per_message": 0.0,
                        "is_lazy_spam": False,
                        "profile_summary": "",
                    },
                    "english": {
                        "message_count": 0,
                        "average_words_per_message": 0.0,
                        "is_lazy_spam": False,
                        "profile_summary": "",
                    },
                },
                "wellness": {"workouts_completed": 0, "activity_minutes": 0, "recommendations": []},
            },
        ]

        summary = await orchestrator._generate_summary_text(report_data)

        assert "לא פעלו היום: ליבי" in summary
        assert "לא פעל היום: לביא" not in summary

    async def test_daily_report_strips_cross_child_name_mentions(self, monkeypatch):
        # Regression: the 21/10 report leaked "ונויה מתבקשת לשמור על תזונה"
        # inside לביא's fitness bullet. Any bullet under **X** that names a
        # different child must be dropped.
        import orchestrator
        import tutors

        llm_summary = (
            "📅 דוח יומי - 21/10/2026\n"
            "**נויה**\n"
            "🔤 נויה השתתפה באנגלית.\n"
            "**לביא**\n"
            "📐 לביא הצטיין בחיבור.\n"
            "🏃 אין פעילות גופנית ונויה מתבקשת לשמור על תזונה מאוזנת.\n"
            "**ליבי**\n"
            "📐 ליבי טובה בחשבון פשוט."
        )

        async def fake_call_llm(system_prompt, conversation, max_tokens=None):
            return llm_summary

        monkeypatch.setattr(tutors, "call_llm", fake_call_llm)
        monkeypatch.setattr(orchestrator, "call_llm", fake_call_llm, raising=False)

        def subject_data(count):
            return {
                "message_count": count,
                "average_words_per_message": 2.0,
                "is_lazy_spam": False,
                "profile_summary": "",
            }

        report_data = [
            {
                "child_name": name,
                "grade_level": "כיתה",
                "subjects": {"math": subject_data(5), "english": subject_data(0)},
                "wellness": {"workouts_completed": 0, "activity_minutes": 0, "recommendations": []},
            }
            for name in ("נויה", "לביא", "ליבי")
        ]

        summary = await orchestrator._generate_summary_text(report_data)

        # The mixed-name bullet inside לביא's section must be gone.
        assert "נויה מתבקשת" not in summary
        # Other sections stay intact.
        assert "לביא הצטיין בחיבור" in summary
        assert "ליבי טובה בחשבון פשוט" in summary
        assert "נויה השתתפה באנגלית" in summary


class TestDatabaseMigrations:
    async def test_chat_history_has_required_columns(self, setup_database):
        async with tutor_history_engine.begin() as conn:
            result = await conn.execute(text("PRAGMA table_info(chat_history)"))
            columns = {row[1] for row in result.fetchall()}
        assert "id" in columns
        assert "child_name" in columns
        assert "subject" in columns
        assert "content" in columns
        assert "idempotency_key" in columns

    async def test_student_profiles_has_total_points(self, setup_database):
        # student_profiles lives in the tutor-history database (per-sub-child data).
        async with tutor_history_engine.begin() as conn:
            result = await conn.execute(text("PRAGMA table_info(student_profiles)"))
            columns = {row[1] for row in result.fetchall()}
        assert "total_points" in columns

    async def test_idempotency_unique_index_exists(self, setup_database):
        async with tutor_history_engine.begin() as conn:
            result = await conn.execute(text("PRAGMA index_list(chat_history)"))
            indexes = {row[1] for row in result.fetchall()}
        assert "uq_chat_idempotency" in indexes
