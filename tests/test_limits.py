from datetime import datetime, timedelta

from app.db.models import Clinic, Message
from app.services.limits import SendLimiter, day_start_utc, evaluate_limits
from tests.conftest import NOW


def test_daily_limit_blocks_after_n_approvals():
    allowed = evaluate_limits(
        now=NOW, approvals_today=9, last_approval=None, daily_limit=10, pause_seconds=180
    )
    denied = evaluate_limits(
        now=NOW, approvals_today=10, last_approval=None, daily_limit=10, pause_seconds=180
    )
    assert allowed.allowed
    assert not denied.allowed and "Дневной лимит" in denied.reason


def test_pause_is_enforced_with_exact_boundary():
    last = NOW - timedelta(seconds=179)
    too_soon = evaluate_limits(now=NOW, approvals_today=1, last_approval=last, daily_limit=10, pause_seconds=180)
    assert not too_soon.allowed
    assert too_soon.wait_seconds == 1

    exactly = evaluate_limits(
        now=NOW, approvals_today=1, last_approval=NOW - timedelta(seconds=180), daily_limit=10, pause_seconds=180
    )
    assert exactly.allowed


def test_zero_pause_and_zero_limit_edge_cases():
    assert evaluate_limits(now=NOW, approvals_today=0, last_approval=NOW, daily_limit=10, pause_seconds=0).allowed
    assert not evaluate_limits(now=NOW, approvals_today=0, last_approval=None, daily_limit=0, pause_seconds=0).allowed


def test_day_boundary_uses_tashkent_midnight():
    # 2026-11-06 18:30 UTC is 23:30 in Tashkent (UTC+5): still the 6th
    assert day_start_utc(datetime(2026, 11, 6, 18, 30), "Asia/Tashkent") == datetime(2026, 11, 5, 19, 0)
    # 2026-11-06 19:30 UTC is 00:30 on the 7th in Tashkent: a new day
    assert day_start_utc(datetime(2026, 11, 6, 19, 30), "Asia/Tashkent") == datetime(2026, 11, 6, 19, 0)


async def _approved_message(session, clinic_id: int, when: datetime) -> None:
    session.add(
        Message(
            clinic_id=clinic_id,
            kind="outreach",
            text="t",
            lang="ru",
            version=1,
            status="approved",
            created_at=when,
            approved_at=when,
        )
    )


async def test_db_limiter_counts_only_todays_approvals(session):
    clinic = Clinic(name="Limit Clinic", status="approved", created_at=NOW, updated_at=NOW)
    session.add(clinic)
    await session.flush()
    # ten approvals earlier today (Tashkent), plus one from yesterday that must not count
    for i in range(10):
        await _approved_message(session, clinic.id, NOW - timedelta(hours=2, minutes=i))
    await _approved_message(session, clinic.id, NOW - timedelta(days=1))
    await session.commit()

    limiter = SendLimiter(daily_limit=10, pause_seconds=180, tz_name="Asia/Tashkent")
    decision = await limiter.check(session, NOW + timedelta(hours=1))
    assert not decision.allowed
    assert "Дневной лимит" in decision.reason


async def test_db_limiter_allows_when_under_limit_and_reports_pause(session):
    clinic = Clinic(name="Pause Clinic", status="approved", created_at=NOW, updated_at=NOW)
    session.add(clinic)
    await session.flush()
    await _approved_message(session, clinic.id, NOW)
    await session.commit()

    limiter = SendLimiter(daily_limit=10, pause_seconds=180, tz_name="Asia/Tashkent")
    too_soon = await limiter.check(session, NOW + timedelta(seconds=60))
    assert not too_soon.allowed and too_soon.wait_seconds == 120
    later = await limiter.check(session, NOW + timedelta(seconds=180))
    assert later.allowed
