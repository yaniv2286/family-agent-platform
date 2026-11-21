"""End-to-end child-simulation audit of the math and English tutors.

Every test goes through the real endpoints with a mocked OpenAI client, then
inspects the exact system prompt the LLM received - so we verify what Koko
actually teaches a child, not internal helpers in isolation.
"""
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

APP_PIN = "test-pin-1234"


def _llm_reply(text: str, points: int = 3) -> MagicMock:
    return MagicMock(
        choices=[MagicMock(message=MagicMock(
            content=f'{{"reply_text": "{text}", "points_earned": {points}}}'
        ))],
        usage=MagicMock(model_dump=lambda: {}),
    )


def _tutor_call(mock_openai):
    """Return the tutor's LLM call. The mock sees several LLM calls per
    request (tutor reply, profile update, insight extraction) - the tutor call
    is the one whose system prompt asks for the JSON reply format."""
    for call in mock_openai.chat.completions.create.call_args_list:
        if "reply_text" in call.kwargs["messages"][0]["content"]:
            return call
    return mock_openai.chat.completions.create.call_args


def _last_system_prompt(mock_openai) -> str:
    return _tutor_call(mock_openai).kwargs["messages"][0]["content"]


async def _chat(client, user_id, messages, subject="math", english=False):
    url = "/api/tutor/english" if english else "/api/tutor/chat"
    body = {
        "user_id": user_id,
        "messages": messages,
        "idempotency_key": str(uuid.uuid4()),
    }
    if not english:
        body["subject"] = subject
    resp = await client.post(url, json=body, headers={"x-app-pin": APP_PIN})
    assert resp.status_code == 200
    return resp.json()


class TestMathChildAudit:
    async def test_homework_expression_gets_deterministic_answer(
        self, client, child_user, mock_openai
    ):
        # Noya's real bug: (5+5:5)*5 was taught as 10. The prompt must now
        # carry the deterministic result 30.
        await _chat(client, child_user.id, [
            {"role": "user", "content": "איך פותרים (5+5:5)*5?"},
        ])
        prompt = _last_system_prompt(mock_openai)
        assert "30" in prompt
        assert "חושבה לפי סדר פעולות" in prompt

    async def test_nested_parentheses(self, client, child_user, mock_openai):
        await _chat(client, child_user.id, [
            {"role": "user", "content": "((2+3)*2)-(4:2)"},
        ])
        # (5*2)-2 = 8
        assert "התוצאה הנכונה היא 8" in _last_system_prompt(mock_openai)

    async def test_x_letter_multiplication(self, client, child_user, mock_openai):
        await _chat(client, child_user.id, [
            {"role": "user", "content": "3x4"},
        ])
        assert "התוצאה הנכונה היא 12" in _last_system_prompt(mock_openai)

    async def test_division_by_zero_not_injected(
        self, client, child_user, mock_openai
    ):
        await _chat(client, child_user.id, [
            {"role": "user", "content": "5:0"},
        ])
        # Uncomputable - the LLM handles it conversationally, no fake fact.
        assert "חושבה לפי סדר פעולות" not in _last_system_prompt(mock_openai)

    async def test_huge_exponent_rejected(self, client, child_user, mock_openai):
        # "9**9**9" would hang the worker if eval() ran it.
        await _chat(client, child_user.id, [
            {"role": "user", "content": "9**9**9"},
        ])
        assert "חושבה לפי סדר פעולות" not in _last_system_prompt(mock_openai)

    async def test_clock_time_is_not_division(
        self, client, child_user, mock_openai
    ):
        # "18:30" looks like "18 divided by 30" to a naive parser - it is a
        # time of day and must NOT inject a bogus "result".
        await _chat(client, child_user.id, [
            {"role": "user", "content": "אמא אמרה שב-18:30 צריך לסיים"},
        ])
        assert "חושבה לפי סדר פעולות" not in _last_system_prompt(mock_openai)

    async def test_correct_answer_confirmed_in_prompt(
        self, client, child_user, mock_openai
    ):
        await _chat(client, child_user.id, [
            {"role": "assistant", "content": "כמה זה 8 + 2?"},
            {"role": "user", "content": "10"},
        ])
        prompt = _last_system_prompt(mock_openai)
        assert "ענה/ענתה נכון" in prompt
        assert "8 + 2" in prompt or "8+2" in prompt

    async def test_answer_matching_a_trick_step_is_confirmed(
        self, client, child_user, mock_openai
    ):
        # Yanai's real bug: asked 9x5, tutor showed the x10 trick
        # (5x10=50, 50-5=45), he answered 45 and was told "כמעט הצלחת".
        # The deterministic checker must scan EVERY calculation in the
        # assistant message, not just the first two numbers.
        await _chat(client, child_user.id, [
            {"role": "assistant", "content": "כמה זה 9 כפול 5? הנה טריק: כפל את 5 ב-10 זה נותן 50. עכשיו החסר את המספר עצמו: 50 פחות 5 זה 45."},
            {"role": "user", "content": "45"},
        ])
        prompt = _last_system_prompt(mock_openai)
        assert "ענה/ענתה נכון" in prompt

    async def test_intermediate_trick_result_not_confirmed(
        self, client, child_user, mock_openai
    ):
        # Asked 9x5 (45). The trick mentions 5x10=50 as an intermediate step.
        # If the child answers 50 (the trick's middle number), it must NOT be
        # confirmed - it's not the answer to the actual question.
        await _chat(client, child_user.id, [
            {"role": "assistant", "content": "כמה זה 9 כפול 5? הנה טריק: כפל את 5 ב-10 זה נותן 50."},
            {"role": "user", "content": "50"},
        ])
        prompt = _last_system_prompt(mock_openai)
        assert "ענה/ענתה נכון" not in prompt

    async def test_wrong_answer_not_confirmed(
        self, client, child_user, mock_openai
    ):
        await _chat(client, child_user.id, [
            {"role": "assistant", "content": "כמה זה 8 + 2?"},
            {"role": "user", "content": "9"},
        ])
        assert "ענה/ענתה נכון" not in _last_system_prompt(mock_openai)

    async def test_wrong_answer_gets_wrong_verdict_in_prompt(
        self, client, child_user, mock_openai
    ):
        # Production bug: child answered "3" to 6-2 and KOKO confirmed it.
        # A wrong answer must inject the true result + an explicit "do not
        # praise" fact, not leave grading to the LLM.
        await _chat(client, child_user.id, [
            {"role": "assistant", "content": "כמה זה 6 - 2?"},
            {"role": "user", "content": "3"},
        ])
        prompt = _last_system_prompt(mock_openai)
        assert "התשובה הנכונה היא 4" in prompt
        assert "שגויה" in prompt

    async def test_tutor_gets_larger_token_budget(
        self, client, child_user, mock_openai
    ):
        # Regression: tutor replies were truncated mid-sentence at 200 tokens.
        await _chat(client, child_user.id, [
            {"role": "user", "content": "שלום"},
        ])
        call = _tutor_call(mock_openai)
        budget = call.kwargs.get("max_tokens") or call.kwargs.get("max_completion_tokens")
        assert budget is not None and budget >= 400


class TestEnglishChildAudit:
    async def test_asr_corrupted_answer_marked_correct(
        self, client, child_user, mock_openai
    ):
        # Yanai's real bug: "saw" transcribed as "song" was judged wrong.
        await _chat(client, child_user.id, [
            {"role": "assistant", "content": 'Can you say "I saw a dog today"?'},
            {"role": "user", "content": "I song a dock today"},
        ], english=True)
        prompt = _last_system_prompt(mock_openai)
        assert "בדיקת מערכת" in prompt
        assert "אשר/י את התשובה כנכונה" in prompt

    async def test_partial_pronunciation_gets_targeted_drill(
        self, client, child_user, mock_openai
    ):
        await _chat(client, child_user.id, [
            {"role": "assistant", "content": 'אמרי באנגלית: "the cat is on the table"'},
            {"role": "user", "content": "the cat table"},
        ], english=True)
        prompt = _last_system_prompt(mock_openai)
        assert "בדיקת מערכת" in prompt
        assert "המילים שאולי הוחמצו" in prompt

    async def test_hebrew_question_not_treated_as_wrong_english(
        self, client, child_user, mock_openai
    ):
        resp = await _chat(client, child_user.id, [
            {"role": "user", "content": "אני לא מבינה מה אמרת"},
        ], english=True)
        assert resp["reply"] == "תשובת ניסיון"
        # The prompt must carry the rule that Hebrew mid-lesson is legitimate.
        assert "התלמיד/ה רשאי/ת לדבר בעברית" in _last_system_prompt(mock_openai)

    async def test_english_prompt_demands_json_and_caps(
        self, client, child_user, mock_openai
    ):
        await _chat(client, child_user.id, [
            {"role": "user", "content": "hello"},
        ], english=True)
        prompt = _last_system_prompt(mock_openai)
        assert "reply_text" in prompt
        assert "english.vocabulary.basic" in prompt


class TestReplyParsingAudit:
    """_parse_json_reply must never leak raw JSON or crash on bad model output."""

    @pytest.mark.parametrize("raw,expect_hebrew", [
        ('{}', "ננסה שוב"),
        ('', "ננסה שוב"),
        ('{"reply_text":', "ננסה שוב"),
    ])
    def test_broken_json_gets_friendly_fallback(self, raw, expect_hebrew):
        from tutors import _parse_json_reply
        reply, points, *_ = _parse_json_reply(raw)
        assert expect_hebrew in reply
        assert "{" not in reply

    def test_points_capped_at_five(self):
        from tutors import _parse_json_reply
        _, points, *_ = _parse_json_reply(
            '{"reply_text": "ok", "points_earned": 999}'
        )
        assert points == 5

    def test_garbage_string_passes_through_safely(self):
        from tutors import _parse_json_reply
        reply, points, *_ = _parse_json_reply("סתם טקסט בלי ג'ייסון")
        assert reply == "סתם טקסט בלי ג'ייסון"
        assert points == 1
