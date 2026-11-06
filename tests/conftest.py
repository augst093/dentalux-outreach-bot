"""Shared fixtures. Tests use a real SQLite file (async driver) and never touch the network."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base

NOW = datetime(2026, 11, 6, 10, 0, 0)  # 15:00 in Tashkent
OWNER_ID = 111_111


class FakeClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now = self.now + timedelta(seconds=seconds)


@pytest_asyncio.fixture
async def engine(tmp_path):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
    eng = create_async_engine(url)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
