"""Daily send limit and minimum pause between approvals.

An 'approval' (owner pressed «Да») is the unit that is counted: it is the moment the bot
hands the owner a ready text for a clinic. The owner sends it manually afterwards.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Message


@dataclass(frozen=True)
class LimitDecision:
    allowed: bool
    reason: str = ""
    wait_seconds: int = 0


def day_start_utc(now_utc: datetime, tz_name: str) -> datetime:
    """Start of the local calendar day containing `now_utc`, returned as naive UTC."""
    aware = now_utc.replace(tzinfo=timezone.utc) if now_utc.tzinfo is None else now_utc
    local = aware.astimezone(ZoneInfo(tz_name))
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc).replace(tzinfo=None)


def evaluate_limits(
    *,
    now: datetime,
    approvals_today: int,
    last_approval: datetime | None,
    daily_limit: int,
    pause_seconds: int,
) -> LimitDecision:
    """Pure decision function. Times are naive UTC."""
    if approvals_today >= daily_limit:
        return LimitDecision(
            allowed=False,
            reason=f"Дневной лимит {daily_limit} одобрений исчерпан. Продолжим завтра.",
        )
    if last_approval is not None:
        elapsed = (now - last_approval).total_seconds()
        if elapsed < pause_seconds:
            wait = math.ceil(pause_seconds - elapsed)
            return LimitDecision(
                allowed=False,
                reason=f"Пауза между отправками. Следующее одобрение через {wait} сек.",
                wait_seconds=wait,
            )
    return LimitDecision(allowed=True)


class SendLimiter:
    def __init__(self, *, daily_limit: int, pause_seconds: int, tz_name: str) -> None:
        self.daily_limit = daily_limit
        self.pause_seconds = pause_seconds
        self.tz_name = tz_name

    async def check(self, session: AsyncSession, now: datetime) -> LimitDecision:
        start = day_start_utc(now, self.tz_name)
        approvals_today = (
            await session.execute(
                select(func.count(Message.id)).where(Message.approved_at.is_not(None), Message.approved_at >= start)
            )
        ).scalar_one()
        last = (await session.execute(select(func.max(Message.approved_at)))).scalar()
        return evaluate_limits(
            now=now,
            approvals_today=int(approvals_today),
            last_approval=last,
            daily_limit=self.daily_limit,
            pause_seconds=self.pause_seconds,
        )
