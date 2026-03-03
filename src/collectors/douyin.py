from __future__ import annotations

"""
抖音直播弹幕采集器

协议：WebSocket + Protobuf（自定义二进制协议）
签名：sign.js (MiniRacer) + ac_signature.py，无需 Playwright/Cookie

连接流程：
  1. GET https://live.douyin.com/ → 获取 ttwid cookie（自动）
  2. GET https://live.douyin.com/{web_rid} → 正则提取 room_id（自动）
  3. 构建 WSS URL，用 sign.js 计算 signature 参数
  4. 建立 WebSocket 连接到 webcast100-ws-web-lq.douyin.com
  5. 接收 PushFrame → gzip 解压 → Protobuf Response → 分发消息
  6. 定时发送心跳帧 + ACK 回复
  7. 断线由 BaseCollector 自动重连
"""

import asyncio
import gzip
import hashlib
import json
import logging
import random
import re
import string
import time
import urllib.parse
from pathlib import Path

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from src.collectors.base import BaseCollector
from src.database.models import MessageType, Session

# ── Protobuf 延迟导入 ─────────────────────────────────────────────────────────
try:
    from src.proto import douyin_pb2 as pb  # type: ignore
    _HAS_PROTO = True
except ImportError:
    _HAS_PROTO = False

# ── 签名依赖（execjs + Node.js）──────────────────────────────────────────────
try:
    import execjs
    _HAS_EXECJS = True
except ImportError:
    _HAS_EXECJS = False

logger = logging.getLogger(__name__)

# ── 签名文件路径 ──────────────────────────────────────────────────────────────
_SIGN_DIR  = Path(__file__).parent.parent / "douyin_sign"
_SIGN_JS   = _SIGN_DIR / "sign.js"
_BOGUS_JS  = _SIGN_DIR / "a_bogus.js"

# ── 常量 ──────────────────────────────────────────────────────────────────────
_LIVE_HOST   = "https://live.douyin.com"
_WS_URL_BASE = (
    "wss://webcast100-ws-web-lq.douyin.com/webcast/im/push/v2/"
    "?app_name=douyin_web"
    "&version_code=180800"
    "&webcast_sdk_version=1.0.14-beta.0"
    "&update_version_code=1.0.14-beta.0"
    "&compress=gzip"
    "&device_platform=web"
    "&cookie_enabled=true"
    "&screen_width=1536&screen_height=864"
    "&browser_language=zh-CN"
    "&browser_platform=Win32"
    "&browser_name=Mozilla"
    "&browser_version=5.0%20(Windows%20NT%2010.0;%20Win64;%20x64)%20"
    "AppleWebKit/537.36%20(KHTML,%20like%20Gecko)%20Chrome/126.0.0.0%20Safari/537.36"
    "&browser_online=true"
    "&tz_name=Asia/Shanghai"
    "&cursor=d-1_u-1_fh-7392091211001140287_t-1721106114633_r-1"
    "&did_rule=3"
    "&aid=6383"
    "&live_id=1"
    "&endpoint=live_pc"
    "&support_wrds=1"
    "&im_path=/webcast/im/fetch/"
    "&identity=audience"
    "&need_persist_msg_count=15"
    "&heartbeatDuration=0"
)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

_HEARTBEAT_INTERVAL = 5   # 秒，与原项目保持一致


def _generate_ms_token(length: int = 182) -> str:
    """生成随机 msToken（182 位字母数字）"""
    base = string.ascii_letters + string.digits + "-_"
    return "".join(random.choice(base) for _ in range(length))


def _generate_signature(wss_url: str) -> str:
    """
    用 sign.js 计算 WebSocket URL 的 signature 参数。
    逻辑：从 URL 提取特定字段 → MD5 → execjs(Node.js) 调用 get_sign(md5)
    """
    if not _HAS_EXECJS:
        logger.warning("[Douyin] PyExecJS 未安装，签名跳过，连接可能失败")
        return ""
    if not _SIGN_JS.exists():
        logger.warning("[Douyin] sign.js 不存在，签名跳过")
        return ""

    param_keys = (
        "live_id,aid,version_code,webcast_sdk_version,"
        "room_id,sub_room_id,sub_channel_id,did_rule,"
        "user_unique_id,device_platform,device_type,ac,"
        "identity"
    ).split(",")

    parsed   = urllib.parse.urlparse(wss_url)
    qs_map   = dict(urllib.parse.parse_qsl(parsed.query))
    tpl_list = [f"{k}={qs_map.get(k, '')}" for k in param_keys]
    param    = ",".join(tpl_list)

    md5 = hashlib.md5(param.encode()).hexdigest()

    script = _SIGN_JS.read_text(encoding="utf-8")
    ctx    = execjs.compile(script)
    return ctx.call("get_sign", md5)


class DouyinCollector(BaseCollector):
    """
    抖音直播弹幕采集器（签名直连版）。

    用法::

        collector = DouyinCollector(
            session=session,
            queue=queue,
            room_id="7123456789",   # 直播间 web_rid（URL 中的数字）
            cookie="",              # 可空，ttwid 自动获取
        )
        await collector.start()
    """

    def __init__(
        self,
        session:  Session,
        queue:    asyncio.Queue,
        room_id:  str,
        cookie:   str = "",
        web_rid:  str = "",
    ) -> None:
        super().__init__(session, queue)
        self._web_rid    = web_rid or room_id
        self._room_id    = room_id
        self._cookie     = cookie
        self._ttwid: str = ""
        self._ws: websockets.WebSocketClientProtocol | None = None

    # ── 获取 ttwid（访问直播间首页，从 Cookie 提取）────────────────────────────

    async def _fetch_ttwid(self) -> str:
        """访问直播间首页，自动提取 ttwid cookie。"""
        headers = {
            "User-Agent": _USER_AGENT,
            "Referer": "https://live.douyin.com/",
        }
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
                resp = await client.get(_LIVE_HOST + "/", headers=headers)
                ttwid = resp.cookies.get("ttwid", "")
                if ttwid:
                    logger.info("[Douyin] ttwid 获取成功")
                    return ttwid
        except Exception as exc:
            logger.warning("[Douyin] 获取 ttwid 失败: %s", exc)
        return ""

    # ── 获取真实 room_id（从直播间页面 HTML 提取）────────────────────────────

    async def _resolve_room_id(self) -> None:
        """通过访问直播间页面，从 HTML 中提取真实 room_id。"""
        url = f"{_LIVE_HOST}/{self._web_rid}"
        ms_token = _generate_ms_token()
        ttwid = self._ttwid or await self._fetch_ttwid()
        if ttwid:
            self._ttwid = ttwid

        headers = {
            "User-Agent": _USER_AGENT,
            "Cookie": f"ttwid={self._ttwid}&msToken={ms_token}; __ac_nonce=0123407cc00a9e438deb4",
            "Referer": f"{_LIVE_HOST}/",
        }
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
                resp = await client.get(url, headers=headers)
                html = resp.text
        except Exception as exc:
            logger.warning("[Douyin] 页面请求失败: %s", exc)
            return

        match = re.search(r'roomId\\":\\"(\d+)\\"', html)
        if match:
            self._room_id = match.group(1)
            logger.info("[Douyin] room_id 解析成功: %s → %s", self._web_rid, self._room_id)
        else:
            # 降级：多种 regex 兜底
            for pat in (
                re.compile(r'"roomId"\s*:\s*"(\d+)"'),
                re.compile(r'"room_id"\s*:\s*"(\d+)"'),
                re.compile(r'roomId=(\d+)'),
            ):
                m = pat.search(html)
                if m:
                    self._room_id = m.group(1)
                    logger.info("[Douyin] room_id 解析（fallback）: %s", self._room_id)
                    return
            logger.warning("[Douyin] 未能从页面提取 room_id，将使用 web_rid=%s", self._web_rid)

    # ── BaseCollector 接口 ────────────────────────────────────────────────────

    async def _connect(self) -> None:
        # 1. 自动获取 ttwid
        if not self._ttwid:
            self._ttwid = await self._fetch_ttwid()

        # 2. 解析真实 room_id
        await self._resolve_room_id()

        # 3. 构建 WSS URL
        ws_url = self._build_ws_url()

        # 4. Cookie：优先用用户提供的，否则用自动获取的 ttwid
        cookie_str = self._cookie or f"ttwid={self._ttwid}"

        headers = {
            "User-Agent": _USER_AGENT,
            "Cookie": cookie_str,
            "Referer": f"{_LIVE_HOST}/{self._web_rid}",
        }

        logger.info("[Douyin] 连接直播间 room_id=%s web_rid=%s", self._room_id, self._web_rid)

        async with websockets.connect(
            ws_url,
            additional_headers=headers,
            ping_interval=None,  # 由 _heartbeat_loop 管理心跳
            max_size=10 * 1024 * 1024,
            open_timeout=20,
        ) as ws:
            self._ws = ws
            logger.info("[Douyin] WebSocket 已连接")

            await asyncio.gather(
                self._recv_loop(ws),
                self._heartbeat_loop(ws),
            )

    # ── 构建 WSS URL ───────────────────────────────────────────────────────────

    def _build_ws_url(self) -> str:
        """构建带 room_id、user_unique_id、internal_ext、signature 的完整 WSS URL。"""
        user_unique_id = str(random.randint(10**18, 10**19 - 1))
        now_ms = int(time.time() * 1000)

        wss = (
            _WS_URL_BASE
            + f"&internal_ext=internal_src:dim"
            + f"|wss_push_room_id:{self._room_id}"
            + f"|wss_push_did:{user_unique_id}"
            + f"|first_req_ms:{now_ms}"
            + f"|fetch_time:{now_ms}"
            + f"|seq:1|wss_info:0-{now_ms}-0-0"
            + f"|wrds_v:7392094459690748497"
            + f"&host=https://live.douyin.com"
            + f"&user_unique_id={user_unique_id}"
            + f"&room_id={self._room_id}"
            + f"&web_rid={self._web_rid}"
        )

        # 计算签名
        try:
            sig = _generate_signature(wss)
            if sig:
                wss += f"&signature={sig}"
                logger.info("[Douyin] 签名计算成功")
            else:
                logger.warning("[Douyin] 签名为空，将以无签名方式尝试连接")
        except Exception as exc:
            logger.warning("[Douyin] 签名计算失败: %s", exc)

        return wss

    # ── WebSocket 循环 ────────────────────────────────────────────────────────

    async def _recv_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        async for raw in ws:
            if not self._running:
                break
            if isinstance(raw, bytes):
                await self._handle_frame(ws, raw)

    async def _heartbeat_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        """每 5 秒发送一次 protobuf 心跳（payload_type='hb'）。"""
        while self._running:
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            try:
                if _HAS_PROTO:
                    frame = pb.PushFrame()
                    frame.payloadType = "hb"
                    await ws.send(frame.SerializeToString())
                else:
                    await ws.ping()
            except ConnectionClosed:
                break
            except Exception as exc:
                logger.warning("[Douyin] 心跳错误: %s", exc)

    # ── 帧解析 ────────────────────────────────────────────────────────────────

    async def _handle_frame(self, ws: websockets.WebSocketClientProtocol, raw: bytes) -> None:
        if not _HAS_PROTO:
            return

        try:
            frame = pb.PushFrame()
            frame.ParseFromString(raw)

            payload = frame.payload
            # 不管 payloadEncoding 字段值，只要以 gzip 魔术字节开头就解压
            if frame.payloadEncoding == "gzip" or payload[:2] == b"\x1f\x8b":
                payload = gzip.decompress(payload)

            response = pb.Response()
            response.ParseFromString(payload)

            # ACK 回复
            if response.needAck:
                ack = pb.PushFrame()
                ack.logId       = frame.logId
                ack.payloadType = "ack"
                ack.payload     = response.internalExt.encode("utf-8")
                await ws.send(ack.SerializeToString())

            for msg in response.messagesList:
                self._dispatch(msg)

        except Exception as exc:
            logger.debug("[Douyin] 帧解析错误: %s", exc)

    def _dispatch(self, msg) -> None:
        handlers = {
            "WebcastChatMessage":   self._on_chat,
            "WebcastGiftMessage":   self._on_gift,
            "WebcastMemberMessage": self._on_member,
            "WebcastLikeMessage":   self._on_like,
            "WebcastSocialMessage": self._on_social,
        }
        handler = handlers.get(msg.method)
        if handler:
            try:
                handler(msg.payload, msg.msgId)
            except Exception as exc:
                logger.debug("[Douyin] Handler %s error: %s", msg.method, exc)

    # ── 消息处理 ──────────────────────────────────────────────────────────────

    def _on_chat(self, payload: bytes, msg_id: int) -> None:
        msg = pb.ChatMessage()
        msg.ParseFromString(payload)
        self._emit(
            msg_type=MessageType.CHAT,
            user_id=str(msg.user.id),
            username=msg.user.nickName,
            content=msg.content,
            msg_id=str(msg_id),
        )
        logger.debug("[Douyin] 💬 %s: %s", msg.user.nickName, msg.content)

    def _on_gift(self, payload: bytes, msg_id: int) -> None:
        msg = pb.GiftMessage()
        msg.ParseFromString(payload)
        extra = json.dumps(
            {"gift_id": msg.giftId, "repeat_count": msg.repeatCount},
            ensure_ascii=False,
        )
        self._emit(
            msg_type=MessageType.GIFT,
            user_id=str(msg.user.id),
            username=msg.user.nickName,
            content=msg.gift.name,
            msg_id=str(msg_id),
            extra=extra,
        )
        logger.debug("[Douyin] 🎁 %s sent %s x%d", msg.user.nickName, msg.gift.name, msg.repeatCount)

    def _on_member(self, payload: bytes, msg_id: int) -> None:
        msg = pb.MemberMessage()
        msg.ParseFromString(payload)
        self._emit(
            msg_type=MessageType.ENTER,
            user_id=str(msg.user.id),
            username=msg.user.nickName,
            msg_id=str(msg_id),
        )

    def _on_like(self, payload: bytes, msg_id: int) -> None:
        msg = pb.LikeMessage()
        msg.ParseFromString(payload)
        self._emit(
            msg_type=MessageType.LIKE,
            user_id=str(msg.user.id),
            username=msg.user.nickName,
            msg_id=str(msg_id),
        )

    def _on_social(self, payload: bytes, msg_id: int) -> None:
        msg = pb.SocialMessage()
        msg.ParseFromString(payload)
        self._emit(
            msg_type=MessageType.SUBSCRIBE,
            user_id=str(msg.user.id),
            username=msg.user.nickName,
            msg_id=str(msg_id),
        )

    # ── 静态工具：从 URL 提取 web_rid ────────────────────────────────────────

    @staticmethod
    async def fetch_room_id(live_url: str, cookie: str = "") -> str | None:
        """从抖音直播间 URL 提取 web_rid（URL 路径中的数字）。"""
        path_part = live_url.rstrip("/").split("/")[-1].split("?")[0]
        if path_part.isdigit():
            return path_part

        headers = {
            "User-Agent": _USER_AGENT,
            "Referer": "https://live.douyin.com/",
        }
        if cookie:
            headers["Cookie"] = cookie

        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
                resp = await client.get(live_url, headers=headers)
                final_path = str(resp.url).rstrip("/").split("/")[-1].split("?")[0]
                if final_path.isdigit():
                    return final_path
        except Exception as exc:
            logger.warning("[Douyin] fetch_room_id 请求失败: %s", exc)

        return None
