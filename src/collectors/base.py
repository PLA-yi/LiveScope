from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections import OrderedDict
from datetime import datetime

from src.config import config
from src.database.models import Message, MessageType, Session

logger = logging.getLogger(__name__)


class BaseCollector(ABC):
    """
    所有平台采集器的抽象基类。

    子类实现 `_connect()` 即可，消息统一通过 `_emit()` 推入队列。
    内置：
      - LRU 去重（基于平台消息 ID）
      - 自动指数退避重连
      - session 生命周期管理
    """

    MAX_RETRIES = 10
    BASE_BACKOFF = 2.0   # 秒
    MAX_BACKOFF = 120.0  # 秒

    def __init__(
        self,
        session: Session,
        queue: asyncio.Queue[Message],
    ) -> None:
        self.session = session
        self._queue = queue
        self._running = False
        self._dedup: OrderedDict[str, None] = OrderedDict()
        self._dedup_size = config.DEDUP_CACHE_SIZE
        self._msg_count = 0

    # ── Public API ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """启动采集，带自动重连。"""
        self._running = True
        retries = 0
        backoff = self.BASE_BACKOFF

        while self._running and retries <= self.MAX_RETRIES:
            try:
                logger.info(
                    "[%s] Connecting to %s …",
                    self.__class__.__name__,
                    self.session.streamer_id,
                )
                await self._connect()
                retries = 0
                backoff = self.BASE_BACKOFF
            except asyncio.CancelledError:
                break
            except Exception as exc:
                retries += 1
                logger.warning(
                    "[%s] Connection error (#%d/%d): %s. Retrying in %.0fs …",
                    self.__class__.__name__,
                    retries,
                    self.MAX_RETRIES,
                    exc,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.MAX_BACKOFF)

        if retries > self.MAX_RETRIES:
            logger.error("[%s] Max retries reached. Giving up.", self.__class__.__name__)

    async def stop(self) -> None:
        self._running = False
        logger.info("[%s] Stopped. Total messages: %d", self.__class__.__name__, self._msg_count)

    # ── Subclass Interface ────────────────────────────────────────────────────

    @abstractmethod
    async def _connect(self) -> None:
        """建立连接并持续接收消息，直到断开或 self._running=False。"""

    # ── Helpers for subclasses ────────────────────────────────────────────────

    def _emit(
        self,
        msg_type: MessageType,
        user_id: str = "",
        username: str = "",
        content: str = "",
        msg_id: str = "",
        extra: str = "",
    ) -> None:
        """将消息放入队列（自动去重）。"""
        # 去重
        if msg_id:
            if msg_id in self._dedup:
                return
            self._dedup[msg_id] = None
            if len(self._dedup) > self._dedup_size:
                self._dedup.popitem(last=False)

        msg = Message(
            session_id=self.session.id,
            msg_id=msg_id or None,
            msg_type=msg_type,
            user_id=user_id,
            username=username,
            content=content,
            extra=extra or None,
            timestamp=datetime.utcnow(),
        )
        try:
            self._queue.put_nowait(msg)
            self._msg_count += 1
        except asyncio.QueueFull:
            logger.warning("Message queue is full, dropping message.")
