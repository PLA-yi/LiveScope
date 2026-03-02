from __future__ import annotations

import asyncio
import logging
from collections import deque
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import config
from src.database.models import Base, Message, Session

logger = logging.getLogger(__name__)


# ── Engine & Session factory ──────────────────────────────────────────────────

engine = create_async_engine(
    config.DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False},
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def init_db() -> None:
    """创建所有表（首次启动时调用）"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database initialized.")


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ── Session CRUD ──────────────────────────────────────────────────────────────

async def upsert_live_session(session_obj: Session) -> None:
    async with get_session() as db:
        await db.merge(session_obj)


async def end_live_session(session_id: str) -> None:
    from sqlalchemy import update
    from datetime import datetime

    async with get_session() as db:
        await db.execute(
            update(Session)
            .where(Session.id == session_id)
            .values(status="ended", ended_at=datetime.utcnow())
        )


# ── Batch Message Writer ──────────────────────────────────────────────────────

class BatchWriter:
    """
    消费 asyncio.Queue 中的消息，按时间间隔批量写入数据库，
    避免每条消息单独 IO。
    """

    def __init__(self, queue: asyncio.Queue[Message], interval: float = config.BATCH_WRITE_INTERVAL):
        self._queue = queue
        self._interval = interval
        self._running = False
        self._task: asyncio.Task | None = None
        self._total_written = 0

    def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="batch-writer")
        logger.info("BatchWriter started (interval=%.1fs)", self._interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        # 把队列剩余消息全部写完
        await self._flush()
        logger.info("BatchWriter stopped. Total written: %d", self._total_written)

    async def _loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._interval)
            await self._flush()

    async def _flush(self) -> None:
        batch: list[Message] = []
        while not self._queue.empty():
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        if not batch:
            return

        try:
            async with get_session() as db:
                db.add_all(batch)
            self._total_written += len(batch)
            logger.debug("Flushed %d messages to DB (total=%d)", len(batch), self._total_written)
        except Exception as e:
            logger.error("BatchWriter flush error: %s", e)
