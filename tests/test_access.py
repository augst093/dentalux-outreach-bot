from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Chat, Message, User

from app.bot.handlers import REFUSAL_TEXT, OwnerOnlyMiddleware
from tests.conftest import OWNER_ID


def _message() -> Message:
    return Message(message_id=1, date=datetime(2026, 11, 6), chat=Chat(id=1, type="private"), text="статус")


@pytest.fixture
def captured(monkeypatch):
    answers: list[str] = []

    async def fake_answer(self, text, *args, **kwargs):
        answers.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer)
    return answers


async def test_non_owner_gets_short_refusal_and_handler_is_not_called(captured):
    middleware = OwnerOnlyMiddleware(admin_user_id=OWNER_ID)
    handler = AsyncMock(return_value="handled")
    stranger = User(id=999, is_bot=False, first_name="x")

    result = await middleware(handler, _message(), {"event_from_user": stranger})

    assert result is None
    handler.assert_not_awaited()
    assert captured == [REFUSAL_TEXT]
    # the refusal must not reveal any commands
    assert "список" not in REFUSAL_TEXT and "статус" not in REFUSAL_TEXT


async def test_missing_user_is_rejected(captured):
    middleware = OwnerOnlyMiddleware(admin_user_id=OWNER_ID)
    handler = AsyncMock()
    await middleware(handler, _message(), {})
    handler.assert_not_awaited()
    assert captured == [REFUSAL_TEXT]


async def test_owner_passes_through(captured):
    middleware = OwnerOnlyMiddleware(admin_user_id=OWNER_ID)
    handler = AsyncMock(return_value="handled")
    owner = User(id=OWNER_ID, is_bot=False, first_name="owner")

    result = await middleware(handler, _message(), {"event_from_user": owner})

    assert result == "handled"
    handler.assert_awaited_once()
    assert captured == []
