"""End-to-end owner flows through the Operator, on a real SQLite database. No Telegram, no network."""

from decimal import Decimal

from sqlalchemy import select

from app.db.models import Clinic, Event, Message
from app.services.drafts import DraftGenerator
from app.services.limits import SendLimiter
from app.services.operator import Operator
from tests.conftest import FakeClock


def _operator(session, clock: FakeClock, *, daily: int = 10, pause: int = 180, llm=None) -> Operator:
    return Operator(
        session,
        generator=DraftGenerator(llm=llm),  # no key: templates
        limiter=SendLimiter(daily_limit=daily, pause_seconds=pause, tz_name="Asia/Tashkent"),
        clock=clock,
    )


async def _add(op: Operator, name: str, contact: str | None = "@clinic_" , direction="импланты"):
    contact = contact if contact is None else contact + name.split()[0].lower()
    return await op.create_clinic(name=name, direction=direction, public_contact=contact, note="сайт про импланты")


async def _clinic_by_name(session, name: str) -> Clinic:
    return (await session.execute(select(Clinic).where(Clinic.name == name))).scalar_one()


async def _reply_texts(replies) -> str:
    return "\n".join(r.text for r in replies)


async def test_add_clinic_creates_ready_draft_and_focus(session):
    clock = FakeClock()
    op = _operator(session, clock)
    replies = await _add(op, "Dental One")
    await session.commit()

    assert "Черновик готов" in replies[0].text
    card = replies[1]
    assert "Dental One" in card.text
    assert [label for label, _ in card.buttons[0]] == ["Отправить (одобрить)", "Пропустить"]

    clinic = await _clinic_by_name(session, "Dental One")
    assert clinic.status == "ready"


async def test_yes_gives_text_and_contact_but_sends_nothing_and_marks_approved(session):
    clock = FakeClock()
    op = _operator(session, clock)
    await _add(op, "Dental One")
    replies = await op.handle_text("Да")
    await session.commit()

    assert "Одобрено: Dental One" in replies[0].text
    assert "отправлено Dental One" in replies[0].text
    assert replies[1].text  # the ready-to-copy text
    clinic = await _clinic_by_name(session, "Dental One")
    assert clinic.status == "approved"


async def test_no_skips_clinic(session):
    clock = FakeClock()
    op = _operator(session, clock)
    await _add(op, "Dental One")
    await op.handle_text("Нет")
    await session.commit()
    assert (await _clinic_by_name(session, "Dental One")).status == "skipped"


async def test_yes_without_any_draft_in_focus_is_explained(session):
    op = _operator(session, FakeClock())
    replies = await op.handle_text("Да")
    assert "нет черновика" in replies[0].text.lower()


async def test_list_shows_pending_cards_with_buttons(session):
    clock = FakeClock()
    op = _operator(session, clock)
    await _add(op, "Dental One")
    await _add(op, "Dental Two")
    replies = await op.handle_text("список кому пишешь")
    assert "Dental One" in replies[1].text and "Dental Two" in replies[2].text
    assert all(r.buttons for r in replies[1:])


async def test_daily_limit_blocks_yes_and_keeps_draft_in_queue(session):
    clock = FakeClock()
    op = _operator(session, clock, daily=1, pause=0)
    await _add(op, "Dental One")
    await _add(op, "Dental Two")
    await op.handle_text("список кому пишешь")  # focus = first card, Dental One
    await op.handle_text("Да")  # uses the only slot of the day
    await session.commit()

    await op.handle_text("список кому пишешь")  # focus moves to Dental Two
    replies = await op.handle_text("Да")
    await session.commit()
    assert "Дневной лимит" in replies[0].text
    assert (await _clinic_by_name(session, "Dental Two")).status == "ready"


async def test_pause_between_approvals_is_enforced_and_expires(session):
    clock = FakeClock()
    op = _operator(session, clock, daily=10, pause=180)
    await _add(op, "Dental One")
    await _add(op, "Dental Two")
    await op.handle_text("список кому пишешь")  # focus = Dental One
    await op.handle_text("Да")
    await session.commit()

    clock.advance(60)
    await op.handle_text("список кому пишешь")  # focus = Dental Two
    blocked = await op.handle_text("Да")
    assert "Пауза" in blocked[0].text and "120" in blocked[0].text

    clock.advance(120)  # 180 s since the first approval
    allowed = await op.handle_text("Да")
    await session.commit()
    assert "Одобрено: Dental Two" in allowed[0].text


async def test_button_approve_uses_same_rules(session):
    clock = FakeClock()
    op = _operator(session, clock)
    replies = await _add(op, "Dental One")
    message_id = int(replies[1].buttons[0][0][1].split(":")[1])
    result = await op.handle_callback(f"ok:{message_id}")
    await session.commit()
    assert "Одобрено" in result[0].text


async def test_redo_creates_new_version_and_rejects_old(session):
    clock = FakeClock()
    op = _operator(session, clock)
    await _add(op, "Dental One")
    old_id = (await session.execute(select(Message.id).order_by(Message.id))).scalars().first()
    replies = await op.handle_text("Переделай: короче")
    await session.commit()

    assert replies[-1].text
    old = await session.get(Message, old_id)
    assert old.status == "rejected"
    versions = (await session.execute(select(Message.version).order_by(Message.version))).scalars().all()
    assert versions == [1, 2]


async def test_redo_can_switch_to_uzbek_latin(session):
    clock = FakeClock()
    op = _operator(session, clock)
    await _add(op, "Dental One")
    await op.handle_text("Переделай: на узбекском")
    await session.commit()
    latest = (
        await session.execute(select(Message).order_by(Message.id.desc()).limit(1))
    ).scalar_one()
    assert latest.lang == "uz"
    assert "yordamchi" in latest.text.lower()


async def test_reply_flow_requires_sent_then_yes_moves_to_negotiating(session):
    clock = FakeClock()
    op = _operator(session, clock, pause=0)
    await _add(op, "Dental One")
    await op.handle_text("Да")
    await session.commit()

    early = await op.handle_text("ответ Dental One: Спасибо, пришлите подробности")
    assert "отправлено" in early[0].text.lower()

    await op.handle_text("отправлено Dental One")
    await session.commit()
    assert (await _clinic_by_name(session, "Dental One")).status == "sent"

    reply = await op.handle_text("ответ Dental One: Спасибо, пришлите подробности?")
    await session.commit()
    assert (await _clinic_by_name(session, "Dental One")).status == "replied"
    assert "Черновик ответа" in reply[0].text or "Ответ записан" in reply[0].text

    approval = await op.handle_text("Да")
    await session.commit()
    assert "Одобрено" in approval[0].text
    assert (await _clinic_by_name(session, "Dental One")).status == "negotiating"


async def test_reply_asking_to_stop_triggers_opt_out_and_blocks_everything(session):
    clock = FakeClock()
    op = _operator(session, clock)
    await _add(op, "Dental One")
    await op.handle_text("Да")
    await op.handle_text("отправлено Dental One")
    await session.commit()

    result = await op.handle_text("ответ Dental One: Не пишите нам больше, спасибо")
    await session.commit()
    assert "opt_out" in result[0].text
    clinic = await _clinic_by_name(session, "Dental One")
    assert clinic.status == "opt_out"

    drafts = (await session.execute(select(Message).where(Message.kind == "reply"))).scalars().all()
    assert drafts == []  # no answer draft is created for an opt-out
    events = (await session.execute(select(Event.type).where(Event.clinic_id == clinic.id))).scalars().all()
    assert "opt_out" in events and "reply_received" in events

    later = await op.handle_text("Да")
    assert "уже обработано" in later[0].text


async def test_reply_from_already_opted_out_clinic_is_logged_not_rejected(session):
    clock = FakeClock()
    op = _operator(session, clock)
    await _add(op, "Dental One")
    await op.handle_text("opt_out Dental One")
    result = await op.handle_text("ответ Dental One: Спасибо, интересно")
    await session.commit()

    assert "уже в статусе opt_out" in result[0].text
    assert (await _clinic_by_name(session, "Dental One")).status == "opt_out"
    logged = (
        await session.execute(select(Event.payload).where(Event.type == "reply_received"))
    ).scalars().all()
    assert len(logged) == 1


async def test_opt_out_command_closes_open_drafts(session):
    clock = FakeClock()
    op = _operator(session, clock)
    await _add(op, "Dental One")
    result = await op.handle_text("opt_out Dental One")
    await session.commit()
    assert "opt_out" in result[0].text
    message = (await session.execute(select(Message))).scalars().one()
    assert message.status == "rejected"
    again = await op.handle_text("opt_out Dental One")
    assert "уже" in again[0].text


async def test_payments_and_status_summary(session):
    clock = FakeClock()
    op = _operator(session, clock, pause=0)
    await _add(op, "Dental One")
    await op.handle_text("Да")
    await op.handle_text("отправлено Dental One")
    await session.commit()

    blocked = await op.handle_text("предоплата Dental One: 250")
    assert "Записано" in blocked[0].text  # sent -> prepaid is allowed

    await op.handle_text("запуск Dental One")
    await op.handle_text("подписка Dental One: 50$")
    await session.commit()

    status = await op.handle_text("статус")
    text = status[0].text
    assert "Клиник в трекере: 1" in text
    assert "Предоплаты" in text and "250" in text
    assert "Запуски: 200" in text and "подписки: 50" in text


async def test_prepayment_amount_and_clinic_errors_are_friendly(session):
    op = _operator(session, FakeClock())
    await _add(op, "Dental One")
    assert "не распознана" in (await op.handle_text("предоплата Dental One: много"))[0].text
    assert "не найдена" in (await op.handle_text("opt_out Nobody"))[0].text
    assert "Формат" in (await op.handle_text("ответ Dental One спасибо"))[0].text


async def test_clinic_without_public_contact_cannot_be_approved(session):
    clock = FakeClock()
    op = _operator(session, clock)
    await op.create_clinic(name="Quiet", direction=None, public_contact=None, note=None)
    result = await op.handle_text("Да")
    assert "публичного контакта" in result[0].text


async def test_unknown_text_and_help(session):
    op = _operator(session, FakeClock())
    assert "не распознана" in (await op.handle_text("абракадабра"))[0].text
    assert "список кому пишешь" in (await op.handle_text("помощь"))[0].text


async def test_missing_api_key_still_produces_drafts_and_tells_owner(session):
    op = _operator(session, FakeClock())
    replies = await _add(op, "Dental One")
    assert "ANTHROPIC_API_KEY" in replies[0].text
    assert replies[1].buttons  # the card is still there and usable


async def test_decimal_formatting_of_payments(session):
    op = _operator(session, FakeClock(), pause=0)
    await _add(op, "Dental One")
    await op.handle_text("Да")
    await op.handle_text("отправлено Dental One")
    reply = await op.handle_text("предоплата Dental One: 250")
    assert "250.00" in reply[0].text
    assert Decimal("250") == Decimal("250.00")
