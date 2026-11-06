"""Drives the real aiogram dispatcher (handlers, owner guard, DB middleware, FSM) with a fake Telegram session.

Nothing goes to the network. Every outgoing Bot API call is recorded so we can check who receives what.
"""

from datetime import datetime

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy import select

from app.bot.handlers import REFUSAL_TEXT, build_dispatcher
from app.db.models import Clinic
from app.services.drafts import DraftGenerator
from app.services.limits import SendLimiter
from app.services.operator import Operator
from tests.conftest import OWNER_ID, FakeClock

BOT_TOKEN = "123456:TEST-TOKEN-FOR-TESTS-ONLY"


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list = []

    async def close(self) -> None:
        return None

    async def stream_content(self, *args, **kwargs):  # abstract in BaseSession; not used by these tests
        raise NotImplementedError
        yield  # pragma: no cover

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=len(self.calls),
                date=datetime(2026, 11, 6),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text,
            )
        if isinstance(method, AnswerCallbackQuery):
            return True
        return True

    def sent_texts(self) -> list[str]:
        return [m.text for m in self.calls if isinstance(m, SendMessage)]


def _make_dispatcher(session_factory, clock: FakeClock) -> tuple[Dispatcher, FakeSession, Bot]:
    fake = FakeSession()
    bot = Bot(token=BOT_TOKEN, session=fake)
    generator = DraftGenerator(api_key=None)
    limiter = SendLimiter(daily_limit=10, pause_seconds=0, tz_name="Asia/Tashkent")

    def make_operator(session):
        return Operator(session, generator=generator, limiter=limiter, clock=clock)

    dp = build_dispatcher(OWNER_ID, session_factory, make_operator)
    return dp, fake, bot


def _text_update(update_id: int, user_id: int, text: str) -> Update:
    user = User(id=user_id, is_bot=False, first_name="p")
    message = Message(
        message_id=update_id,
        date=datetime(2026, 11, 6),
        chat=Chat(id=user_id, type="private"),
        from_user=user,
        text=text,
    )
    return Update(update_id=update_id, message=message)


def _callback_update(update_id: int, user_id: int, data: str) -> Update:
    user = User(id=user_id, is_bot=False, first_name="p")
    message = Message(
        message_id=update_id,
        date=datetime(2026, 11, 6),
        chat=Chat(id=user_id, type="private"),
        from_user=user,
        text="card",
    )
    callback = CallbackQuery(id=str(update_id), from_user=user, chat_instance="1", message=message, data=data)
    return Update(update_id=update_id, callback_query=callback)


async def test_stranger_is_refused_and_gets_no_data(engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(engine, expire_on_commit=False)
    dp, fake, bot = _make_dispatcher(factory, FakeClock())
    await dp.feed_update(bot, _text_update(1, user_id=999, text="статус"))
    assert fake.sent_texts() == [REFUSAL_TEXT]


async def test_owner_add_clinic_wizard_creates_clinic_and_draft(engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(engine, expire_on_commit=False)
    dp, fake, bot = _make_dispatcher(factory, FakeClock())
    steps = ["добавить клинику", "Dental Wizard", "импланты", "@wizard_dental", "-"]
    for i, text in enumerate(steps, start=1):
        await dp.feed_update(bot, _text_update(i, user_id=OWNER_ID, text=text))

    sent = fake.sent_texts()
    assert any("Клиника добавлена: Dental Wizard" in t for t in sent)
    async with factory() as session:
        clinic = (await session.execute(select(Clinic).where(Clinic.name == "Dental Wizard"))).scalar_one()
        assert clinic.status == "ready"
        assert clinic.public_contact == "@wizard_dental"


async def test_owner_approve_by_button_sends_only_to_owner_chat(engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(engine, expire_on_commit=False)
    dp, fake, bot = _make_dispatcher(factory, FakeClock())
    for i, text in enumerate(["добавить клинику", "Dental Btn", "импланты", "@btn_dental", "-"], start=1):
        await dp.feed_update(bot, _text_update(i, user_id=OWNER_ID, text=text))

    async with factory() as session:
        from app.db.models import Message as MessageModel

        message_id = (await session.execute(select(MessageModel.id))).scalars().first()

    await dp.feed_update(bot, _callback_update(50, user_id=OWNER_ID, data=f"ok:{message_id}"))

    sent_calls = [m for m in fake.calls if isinstance(m, SendMessage)]
    assert any("Одобрено: Dental Btn" in (m.text or "") for m in sent_calls)
    # every outgoing message goes to the owner's chat: the bot never writes to clinics
    assert {int(m.chat_id) for m in sent_calls} == {OWNER_ID}


async def test_plain_text_yes_after_list_goes_through_session_and_commits(engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(engine, expire_on_commit=False)
    dp, fake, bot = _make_dispatcher(factory, FakeClock())
    for i, text in enumerate(["добавить клинику", "Dental Yes", "импланты", "@yes_dental", "-"], start=1):
        await dp.feed_update(bot, _text_update(i, user_id=OWNER_ID, text=text))
    await dp.feed_update(bot, _text_update(10, user_id=OWNER_ID, text="Да"))

    async with factory() as session:
        clinic = (await session.execute(select(Clinic).where(Clinic.name == "Dental Yes"))).scalar_one()
        assert clinic.status == "approved"  # committed by the handler, visible in a new session
