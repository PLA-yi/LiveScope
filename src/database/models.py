from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import (
    Column, DateTime, ForeignKey, Integer, String, Text, func
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class Platform(str, Enum):
    TIKTOK = "tiktok"
    DOUYIN = "douyin"


class SessionStatus(str, Enum):
    LIVE = "live"
    ENDED = "ended"


class MessageType(str, Enum):
    CHAT = "chat"
    GIFT = "gift"
    LIKE = "like"
    ENTER = "enter"
    SUBSCRIBE = "subscribe"


class Session(Base):
    """一场直播会话"""
    __tablename__ = "sessions"

    id = Column(String, primary_key=True)              # "{platform}_{room_id}_{timestamp}"
    platform = Column(String, nullable=False)           # Platform 枚举值
    streamer_id = Column(String, nullable=False)        # 主播账号 ID / 用户名
    room_id = Column(String)                            # 直播间 room_id
    title = Column(String)                              # 直播标题
    started_at = Column(DateTime, default=func.now())
    ended_at = Column(DateTime)
    status = Column(String, default=SessionStatus.LIVE)

    messages = relationship("Message", back_populates="session", lazy="dynamic")

    def __repr__(self) -> str:
        return f"<Session {self.platform}:{self.streamer_id} status={self.status}>"


class Message(Base):
    """弹幕/礼物/点赞等消息"""
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String, ForeignKey("sessions.id"), nullable=False, index=True)
    msg_id = Column(String, index=True)                 # 平台原始消息 ID（用于去重）
    msg_type = Column(String, nullable=False)           # MessageType 枚举值
    user_id = Column(String)
    username = Column(String)
    content = Column(Text)                              # 弹幕文本 / 礼物名称等
    extra = Column(Text)                                # JSON，平台特有附加字段
    timestamp = Column(DateTime, default=func.now(), index=True)

    session = relationship("Session", back_populates="messages")

    def __repr__(self) -> str:
        return f"<Message [{self.msg_type}] {self.username}: {self.content[:40] if self.content else ''}>"
