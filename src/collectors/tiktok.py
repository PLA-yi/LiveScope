from __future__ import annotations

import asyncio
import json
import logging

from TikTokLive import TikTokLiveClient
from TikTokLive.events import (
    CommentEvent,
    ConnectEvent,
    DisconnectEvent,
    FollowEvent,
    GiftEvent,
    JoinEvent,
    LikeEvent,
)

from src.collectors.base import BaseCollector
from src.config import config
from src.database.models import MessageType, Session

logger = logging.getLogger(__name__)


class TikTokCollector(BaseCollector):
    def __init__(self, session: Session, queue: asyncio.Queue, unique_id: str, euler_key: str = "") -> None:
        super().__init__(session, queue)
        self._unique_id = unique_id
        self._euler_key = euler_key or config.EULERSTREAM_API_KEY
        self._client: TikTokLiveClient | None = None

    async def _connect(self) -> None:
        kwargs = {}
        if self._euler_key:
            kwargs["web_client_kwargs"] = {"sign_api_key": self._euler_key}
        self._client = TikTokLiveClient(unique_id=self._unique_id, **kwargs)
        self._register_events(self._client)
        await self._client.start()

    def _register_events(self, client: TikTokLiveClient) -> None:

        @client.on(ConnectEvent)
        async def on_connect(event: ConnectEvent) -> None:
            logger.info("[TikTok] Connected to %s", self._unique_id)

        @client.on(DisconnectEvent)
        async def on_disconnect(event: DisconnectEvent) -> None:
            logger.info("[TikTok] Disconnected.")

        @client.on(CommentEvent)
        async def on_comment(event: CommentEvent) -> None:
            # message_id 位于 base_message 中
            mid = str(event.base_message.message_id) if event.base_message else ""
            self._emit(
                msg_type=MessageType.CHAT,
                user_id=str(event.user_info.id) if event.user_info else "",
                username=event.user_info.nick_name if event.user_info else "",
                content=event.content,
                msg_id=mid,
            )
            logger.debug("[TikTok] 💬 %s: %s",
                         event.user_info.nick_name if event.user_info else "?",
                         event.content)

        @client.on(GiftEvent)
        async def on_gift(event: GiftEvent) -> None:
            mid = str(event.base_message.message_id) if event.base_message else ""
            gift_name = event.m_gift.name if event.m_gift else ""
            gift_id   = event.m_gift.id   if event.m_gift else 0
            extra = json.dumps(
                {"gift_id": gift_id, "repeat_count": event.repeat_count},
                ensure_ascii=False,
            )
            user = event.from_user
            self._emit(
                msg_type=MessageType.GIFT,
                user_id=str(user.id) if user else "",
                username=user.nick_name if user else "",
                content=gift_name,
                msg_id=mid,
                extra=extra,
            )
            logger.debug("[TikTok] 🎁 %s sent %s x%d",
                         user.nick_name if user else "?", gift_name, event.repeat_count)

        @client.on(LikeEvent)
        async def on_like(event: LikeEvent) -> None:
            mid = str(event.base_message.message_id) if event.base_message else ""
            user = event.user
            self._emit(
                msg_type=MessageType.LIKE,
                user_id=str(user.id) if user else "",
                username=user.nick_name if user else "",
                msg_id=mid,
            )

        @client.on(JoinEvent)
        async def on_join(event: JoinEvent) -> None:
            mid = str(event.base_message.message_id) if event.base_message else ""
            user = event.user
            self._emit(
                msg_type=MessageType.ENTER,
                user_id=str(user.id) if user else "",
                username=user.nick_name if user else "",
                msg_id=mid,
            )

        @client.on(FollowEvent)
        async def on_follow(event: FollowEvent) -> None:
            mid = str(event.base_message.message_id) if event.base_message else ""
            user = event.user
            self._emit(
                msg_type=MessageType.SUBSCRIBE,
                user_id=str(user.id) if user else "",
                username=user.nick_name if user else "",
                msg_id=mid,
            )
