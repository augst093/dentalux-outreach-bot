from decimal import Decimal

import pytest

from app.db.models import CLINIC_STATUSES, Message
from app.services import tracker
from app.services.tracker import ALLOWED_TRANSITIONS, InvalidTransition, TrackerError
from tests.conftest import NOW


async def _clinic(session, name="Dental Plus", contact="+998901112233", direction="импланты"):
    clinic = await tracker.create_clinic(
        session, name=name, direction=direction, public_contact=contact, note="заметка", now=NOW
    )
    return clinic


async def test_duplicate_clinic_name_is_rejected(session):
    await _clinic(session)
    with pytest.raises(TrackerError):
        await _clinic(session, name="dental plus")


async def test_happy_path_walks_every_status_in_order(session):
    clinic = await _clinic(session)
    for target in ["ready", "approved", "sent", "replied", "negotiating", "prepaid", "launched"]:
        await tracker.change_status(session, clinic, target, NOW)
        assert clinic.status == target
    await session.commit()


async def test_invalid_transition_is_rejected_and_status_unchanged(session):
    clinic = await _clinic(session)
    with pytest.raises(InvalidTransition):
        await tracker.change_status(session, clinic, "sent", NOW)  # new -> sent is not allowed
    assert clinic.status == "new"


async def test_every_status_change_is_written_to_events(session):
    clinic = await _clinic(session)
    await tracker.change_status(session, clinic, "ready", NOW, reason="draft")
    await session.commit()
    from sqlalchemy import select

    from app.db.models import Event

    events = (
        await session.execute(select(Event).where(Event.clinic_id == clinic.id, Event.type == "status_changed"))
    ).scalars().all()
    assert len(events) == 1
    assert '"from": "new"' in events[0].payload and '"to": "ready"' in events[0].payload


async def test_opt_out_is_reachable_from_every_live_status_and_is_terminal():
    for status in CLINIC_STATUSES:
        if status == "opt_out":
            assert ALLOWED_TRANSITIONS[status] == frozenset()
        else:
            assert "opt_out" in ALLOWED_TRANSITIONS[status], status


async def test_opt_out_blocks_new_drafts_and_approval(session):
    clinic = await _clinic(session)
    await tracker.change_status(session, clinic, "ready", NOW)
    draft = await tracker.add_draft(session, clinic, text="черновик", lang="ru", kind="outreach", now=NOW)

    changed = await tracker.opt_out(session, clinic, NOW, reason="test")
    assert changed is True
    assert draft.status == "rejected"  # open drafts are closed
    with pytest.raises(tracker.OptedOutError):
        await tracker.add_draft(session, clinic, text="новый", lang="ru", kind="outreach", now=NOW)
    with pytest.raises(tracker.OptedOutError):
        await tracker.approve_draft(session, draft, clinic, NOW)

    assert await tracker.opt_out(session, clinic, NOW, reason="again") is False


async def test_approve_requires_public_contact(session):
    clinic = await _clinic(session, contact=None)
    await tracker.change_status(session, clinic, "ready", NOW)
    draft = await tracker.add_draft(session, clinic, text="текст", lang="ru", kind="outreach", now=NOW)
    with pytest.raises(TrackerError, match="публичного контакта"):
        await tracker.approve_draft(session, draft, clinic, NOW)
    assert clinic.status == "ready"
    assert draft.status == "draft"


async def test_draft_versions_supersede_each_other(session):
    clinic = await _clinic(session)
    await tracker.change_status(session, clinic, "ready", NOW)
    first = await tracker.add_draft(session, clinic, text="v1", lang="ru", kind="outreach", now=NOW)
    second = await tracker.add_draft(session, clinic, text="v2", lang="ru", kind="outreach", now=NOW)
    assert first.status == "rejected"
    assert second.status == "draft"
    assert (first.version, second.version) == (1, 2)


async def test_reply_requires_sent_first(session):
    clinic = await _clinic(session)
    await tracker.change_status(session, clinic, "ready", NOW)
    await tracker.change_status(session, clinic, "approved", NOW)
    with pytest.raises(TrackerError, match="отправлено"):
        await tracker.record_reply(session, clinic, text="да", now=NOW, opt_out_requested=False)


async def test_mark_sent_moves_clinic_to_sent(session):
    clinic = await _clinic(session)
    await tracker.change_status(session, clinic, "ready", NOW)
    draft = await tracker.add_draft(session, clinic, text="текст", lang="ru", kind="outreach", now=NOW)
    await tracker.approve_draft(session, draft, clinic, NOW)
    await tracker.mark_sent(session, clinic, NOW)
    assert clinic.status == "sent"
    assert draft.status == "sent"


async def test_prepayment_only_after_send_and_is_summed(session):
    clinic = await _clinic(session)
    with pytest.raises(TrackerError):
        await tracker.record_payment(
            session, clinic, amount=Decimal("250"), kind="prepayment", note=None, now=NOW
        )
    await tracker.change_status(session, clinic, "ready", NOW)
    await tracker.change_status(session, clinic, "approved", NOW)
    await tracker.change_status(session, clinic, "sent", NOW)
    await tracker.record_payment(session, clinic, amount=Decimal("250"), kind="prepayment", note=None, now=NOW)
    assert clinic.status == "prepaid"
    stats = await tracker.stats(session)
    assert stats.prepayment_total == Decimal("250")


async def test_launch_needs_prepayment_and_subscription_needs_launch(session):
    clinic = await _clinic(session)
    await tracker.change_status(session, clinic, "ready", NOW)
    await tracker.change_status(session, clinic, "approved", NOW)
    await tracker.change_status(session, clinic, "sent", NOW)
    with pytest.raises(TrackerError):
        await tracker.record_payment(session, clinic, amount=Decimal("200"), kind="launch", note=None, now=NOW)
    with pytest.raises(TrackerError):
        await tracker.record_payment(session, clinic, amount=Decimal("50"), kind="subscription", note=None, now=NOW)
    await tracker.change_status(session, clinic, "prepaid", NOW)
    await tracker.record_payment(session, clinic, amount=Decimal("200"), kind="launch", note=None, now=NOW)
    assert clinic.status == "launched"
    await tracker.record_payment(session, clinic, amount=Decimal("50"), kind="subscription", note=None, now=NOW)
    stats = await tracker.stats(session)
    assert stats.payments_by_kind == {"launch": Decimal("200"), "subscription": Decimal("50")}


def test_parse_amount_accepts_common_formats_and_rejects_nonsense():
    assert tracker.parse_amount("250") == Decimal("250.00")
    assert tracker.parse_amount("250$") == Decimal("250.00")
    assert tracker.parse_amount("250 $") == Decimal("250.00")
    assert tracker.parse_amount("1,5") == Decimal("1.50")
    for bad in ["abc", "0", "-5", "1000000"]:
        with pytest.raises(TrackerError):
            tracker.parse_amount(bad)


async def test_resolve_clinic_handles_unknown_and_ambiguous(session):
    await _clinic(session, name="Smile Clinic")
    await _clinic(session, name="Smile Dental")
    with pytest.raises(TrackerError, match="не найдена"):
        await tracker.resolve_clinic(session, "Nope")
    with pytest.raises(TrackerError, match="несколько"):
        await tracker.resolve_clinic(session, "smile")
    found = await tracker.resolve_clinic(session, "smile dental")
    assert found.name == "Smile Dental"


async def test_messages_are_linked_to_their_clinic(session):
    clinic = await _clinic(session)
    await tracker.change_status(session, clinic, "ready", NOW)
    draft = await tracker.add_draft(session, clinic, text="текст", lang="ru", kind="outreach", now=NOW)
    await session.commit()
    stored = await session.get(Message, draft.id)
    assert stored.clinic_id == clinic.id
