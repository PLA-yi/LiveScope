from __future__ import annotations

"""
抖音直播弹幕采集器

协议：WebSocket + Protobuf（自定义二进制协议）
认证：需要浏览器 Cookie（ttwid / sessionid 等）

连接流程：
  1. HTTP 请求直播间页面，获取 room_id（如果只传了 URL）
  2. 建立 WebSocket 连接到 webcast5-ws-web-hl.douyin.com
  3. 接收 PushFrame，解析 payload（gzip 解压 → Protobuf → Response → Messages）
  4. 针对每个 message.method 分发到对应处理函数
  5. 定时发送心跳帧保持连接
  6. 连接断开时由 BaseCollector 自动重连

注意：抖音的签名算法（X-Bogus / _signature）会定期更新，本实现使用
      Cookie 认证方案规避部分签名校验。如遇 403，请更新 Cookie。
"""

import asyncio
import gzip
import json
import logging
import random
import re
import time
from urllib.parse import urlencode

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from src.collectors.base import BaseCollector
from src.database.models import MessageType, Session

# ── Protobuf 延迟导入（运行时生成）──────────────────────────────────────────
try:
    from src.proto import douyin_pb2 as pb  # type: ignore
    _HAS_PROTO = True
except ImportError:
    _HAS_PROTO = False

logger = logging.getLogger(__name__)

# ── 抖音 WebSocket & API 常量 ────────────────────────────────────────────────
_WS_HOST = "wss://webcast5-ws-web-hl.douyin.com"
_WS_PATH = "/webcast/im/push/v2/"
_ROOM_URL_RE   = re.compile(r'"roomId"\s*:\s*"(\d+)"')
_ROOM_INIT_RE  = re.compile(r'roomId=(\d+)')
_ROOM_DATA_RE  = re.compile(r'"room_id"\s*:\s*"(\d+)"')
_ROOM_LIVE_RE  = re.compile(r'/live/(\d+)')

_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://live.douyin.com/",
    "Origin": "https://live.douyin.com",
}

_HEARTBEAT_INTERVAL = 10  # 秒


class DouyinCollector(BaseCollector):
    """
    抖音直播弹幕采集器。

    用法::

        collector = DouyinCollector(
            session=session,
            queue=queue,
            room_id="7123456789",      # 直播间 room_id
            cookie="ttwid=xxx; ..."    # 浏览器 Cookie（可选但推荐）
        )
        await collector.start()

    room_id 获取方式：
      - 打开直播间页面，在 URL 或页面源码中搜索 "roomId"
      - 也可传入直播间完整 URL，本类会自动提取
    """

    def __init__(
        self,
        session: Session,
        queue: asyncio.Queue,
        room_id: str,
        cookie: str = "",
        web_rid: str = "",
        ws_url: str = "",
    ) -> None:
        super().__init__(session, queue)
        self._web_rid   = web_rid or room_id
        self._room_id   = room_id
        self._cookie    = cookie
        self._fixed_url = ws_url   # 若由前端直接传入完整 WS URL，则跳过自动构建
        self._ws: websockets.WebSocketClientProtocol | None = None

    # ── BaseCollector 接口 ────────────────────────────────────────────────────

    async def _resolve_room_id(self) -> None:
        """通过抖音 HTTP API 将 web_rid 解析为内部 room_id。"""
        headers = dict(_BASE_HEADERS)
        if self._cookie:
            headers["Cookie"] = self._cookie
        params = {
            "aid": "6383",
            "app_name": "douyin_web",
            "live_id": "1",
            "device_platform": "web",
            "language": "zh-CN",
            "web_rid": self._web_rid,
        }
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
                resp = await client.get(
                    "https://live.douyin.com/webcast/room/web/enter/",
                    params=params,
                    headers=headers,
                )
                text = resp.text.strip()
                if not text:
                    raise RuntimeError("直播间不在线或需要 Cookie 认证，请确认直播间正在直播")
                data = resp.json()
            # 检查直播状态
            status_code = data.get("status_code", -1)
            if status_code != 0:
                msg = data.get("message", "未知错误")
                raise RuntimeError(f"直播间 API 返回错误 {status_code}: {msg}")
            room_id = str(data["data"]["data"][0]["id_str"])
            if room_id and room_id != "0":
                logger.info("[Douyin] Resolved room_id: %s → %s", self._web_rid, room_id)
                self._room_id = room_id
        except RuntimeError:
            raise   # 让 BaseCollector 的重试逻辑接管，并打印友好信息
        except Exception as exc:
            logger.warning("[Douyin] Could not resolve room_id via API (%s), using web_rid as fallback", exc)

    @staticmethod
    async def _get_ws_url_via_playwright(web_rid: str, cookie: str = "") -> str | None:
        """
        用独立子进程运行 Playwright，访问直播间并捕获真实 WebSocket URL（含 msToken）。
        子进程与 uvicorn 的 asyncio 完全隔离，避免事件循环冲突。
        """
        import sys
        import json as _json

        # 注：cookie 通过 stdin 传入，避免命令行参数长度限制
        script = r"""
import asyncio, sys, json

async def main():
    data   = json.loads(sys.stdin.read())
    web_rid = data["web_rid"]
    cookie  = data["cookie"]

    captured = []
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        try:
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                locale="zh-CN",
                viewport={"width": 1280, "height": 800},
            )
            if cookie:
                pw_cookies = []
                for item in cookie.split(";"):
                    item = item.strip()
                    if "=" in item:
                        name, _, value = item.partition("=")
                        pw_cookies.append({"name": name.strip(), "value": value.strip(),
                                           "domain": ".douyin.com", "path": "/", "secure": True})
                if pw_cookies:
                    await context.add_cookies(pw_cookies)
            page = await context.new_page()

            # 必须在 goto 之前注册，否则可能错过早期 WebSocket
            def on_ws(ws):
                url = ws.url
                if ("push/v2" in url or ("webcast" in url and "ws" in url)) and not captured:
                    captured.append(url)

            page.on("websocket", on_ws)

            try:
                await page.goto(
                    f"https://live.douyin.com/{web_rid}",
                    wait_until="load",
                    timeout=25000,
                )
            except Exception:
                pass  # timeout/nav errors are non-fatal

            # 等待 JS 初始化并建立 WebSocket（最多 15 秒）
            for _ in range(150):
                if captured:
                    break
                await asyncio.sleep(0.1)

            # 若仍未捕获，再等待 5 秒（处理慢速网络）
            if not captured:
                await asyncio.sleep(5)

        finally:
            await browser.close()

    print(captured[0] if captured else "")

asyncio.run(main())
"""
        logger.warning("[Douyin] Playwright 正在打开直播间 %s …", web_rid)
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", script,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdin_data = _json.dumps({"web_rid": web_rid, "cookie": cookie}).encode()
            stdout, stderr = await asyncio.wait_for(proc.communicate(stdin_data), timeout=55)
            url = stdout.decode().strip()
            if url:
                logger.warning("[Douyin] Playwright 捕获到 WS URL ✓")
                return url
            err_msg = stderr.decode().strip()[-300:] if stderr else ""
            logger.warning("[Douyin] Playwright 未能捕获到 WS URL（直播间可能未在线或未登录）%s",
                           f"\n  stderr: {err_msg}" if err_msg else "")
        except asyncio.TimeoutError:
            logger.warning("[Douyin] Playwright 超时")
            if proc:
                proc.kill()
        except Exception as exc:
            logger.warning("[Douyin] Playwright 出错: %s", exc)
        return None

    async def _connect(self) -> None:
        if self._fixed_url:
            ws_url = self._fixed_url
        else:
            # 优先用 Playwright 全自动获取真实 WS URL（含 msToken）
            ws_url = await self._get_ws_url_via_playwright(self._web_rid, self._cookie)
            if not ws_url:
                # 降级：手动解析 room_id + 构建 URL
                logger.warning("[Douyin] 降级为手动 WS URL 构建")
                await self._resolve_room_id()
                ws_url = self._build_ws_url()
        headers = dict(_BASE_HEADERS)
        if self._cookie:
            headers["Cookie"] = self._cookie

        logger.info("[Douyin] Connecting to room %s (web_rid=%s) …", self._room_id, self._web_rid)

        async with websockets.connect(
            ws_url,
            additional_headers=headers,
            ping_interval=None,  # 我们自己管理心跳
            max_size=10 * 1024 * 1024,  # 10MB，应对大体积帧
            open_timeout=15,
        ) as ws:
            self._ws = ws
            logger.info("[Douyin] WebSocket connected.")

            # 并发：消息接收 + 定时心跳
            await asyncio.gather(
                self._recv_loop(ws),
                self._heartbeat_loop(ws),
            )

    # ── WebSocket 循环 ────────────────────────────────────────────────────────

    async def _recv_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        async for raw in ws:
            if not self._running:
                break
            if isinstance(raw, bytes):
                await self._handle_frame(ws, raw)

    async def _heartbeat_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        """每 10 秒发送一次心跳，保持 WebSocket 连接。"""
        while self._running:
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            try:
                if _HAS_PROTO:
                    frame = pb.PushFrame()
                    frame.payloadType = "hb"
                    await ws.send(frame.SerializeToString())
                else:
                    # 最简心跳：发送空 ping
                    await ws.ping()
            except ConnectionClosed:
                break
            except Exception as exc:
                logger.warning("[Douyin] Heartbeat error: %s", exc)

    # ── 帧解析 ────────────────────────────────────────────────────────────────

    async def _handle_frame(self, ws: websockets.WebSocketClientProtocol, raw: bytes) -> None:
        if not _HAS_PROTO:
            logger.debug("[Douyin] protobuf not compiled, raw frame len=%d", len(raw))
            return

        try:
            frame = pb.PushFrame()
            frame.ParseFromString(raw)

            # 解压 payload（抖音使用 gzip）
            payload = frame.payload
            if frame.payloadEncoding == "gzip":
                payload = gzip.decompress(payload)

            response = pb.Response()
            response.ParseFromString(payload)

            # 如需 ACK，回复服务端
            if response.needAck:
                ack_frame = pb.PushFrame()
                ack_frame.logId = frame.logId
                ack_frame.payloadType = "ack"
                ack_frame.payload = response.internalExt.encode()
                await ws.send(ack_frame.SerializeToString())

            for msg in response.messages:
                self._dispatch(msg)

        except Exception as exc:
            logger.debug("[Douyin] Frame parse error: %s", exc)

    def _dispatch(self, msg) -> None:
        """根据 msg.method 分发到对应处理函数。"""
        method = msg.method
        payload = msg.payload

        handlers = {
            "WebcastChatMessage":   self._on_chat,
            "WebcastGiftMessage":   self._on_gift,
            "WebcastMemberMessage": self._on_member,
            "WebcastLikeMessage":   self._on_like,
            "WebcastSocialMessage": self._on_social,
        }

        handler = handlers.get(method)
        if handler:
            try:
                handler(payload, msg.msgId)
            except Exception as exc:
                logger.debug("[Douyin] Handler %s error: %s", method, exc)

    # ── 消息处理函数 ──────────────────────────────────────────────────────────

    def _on_chat(self, payload: bytes, msg_id: int) -> None:
        chat = pb.ChatMessage()
        chat.ParseFromString(payload)
        self._emit(
            msg_type=MessageType.CHAT,
            user_id=str(chat.user.id),
            username=chat.user.nickName,
            content=chat.content,
            msg_id=str(msg_id),
        )
        logger.debug("[Douyin] 💬 %s: %s", chat.user.nickName, chat.content)

    def _on_gift(self, payload: bytes, msg_id: int) -> None:
        gift = pb.GiftMessage()
        gift.ParseFromString(payload)
        extra = json.dumps(
            {"gift_id": gift.giftId, "repeat_count": gift.repeatCount},
            ensure_ascii=False,
        )
        self._emit(
            msg_type=MessageType.GIFT,
            user_id=str(gift.user.id),
            username=gift.user.nickName,
            content=gift.giftName,
            msg_id=str(msg_id),
            extra=extra,
        )
        logger.debug("[Douyin] 🎁 %s sent %s x%d", gift.user.nickName, gift.giftName, gift.repeatCount)

    def _on_member(self, payload: bytes, msg_id: int) -> None:
        member = pb.MemberMessage()
        member.ParseFromString(payload)
        self._emit(
            msg_type=MessageType.ENTER,
            user_id=str(member.user.id),
            username=member.user.nickName,
            msg_id=str(msg_id),
        )

    def _on_like(self, payload: bytes, msg_id: int) -> None:
        like = pb.LikeMessage()
        like.ParseFromString(payload)
        self._emit(
            msg_type=MessageType.LIKE,
            user_id=str(like.user.id),
            username=like.user.nickName,
            msg_id=str(msg_id),
        )

    def _on_social(self, payload: bytes, msg_id: int) -> None:
        social = pb.SocialMessage()
        social.ParseFromString(payload)
        self._emit(
            msg_type=MessageType.SUBSCRIBE,
            user_id=str(social.user.id),
            username=social.user.nickName,
            msg_id=str(msg_id),
        )

    # ── URL 构造 ──────────────────────────────────────────────────────────────

    def _build_ws_url(self) -> str:
        device_id = str(random.randint(10**18, 10**19 - 1))
        params = {
            "app_name":              "douyin_web",
            "version_code":          "180800",
            "webcast_sdk_version":   "1.0.14",
            "update_version_code":   "1.0.14",
            "compress":              "gzip",
            "device_platform":       "web",
            "cookie_enabled":        "true",
            "http_schema":           "https",
            "aid":                   "6383",
            "device_id":             device_id,
            "live_id":               "1",
            "did_rule":              "3",
            "web_rid":               self._web_rid,
            "room_id_str":           self._room_id,
            "room_id":               self._room_id,
            "identity":              "audience",
            "need_persist_msg_count": "15",
            "support_wrds":          "1",
            "im_path":               "/webcast/im/fetch/",
            "browser_language":      "zh-CN",
            "browser_platform":      "MacIntel",
            "browser_name":          "Chrome",
            "browser_version":       "122.0.0.0",
            "ts":                    str(int(time.time())),
        }
        return f"{_WS_HOST}{_WS_PATH}?{urlencode(params)}"

    # ── 静态工具方法 ──────────────────────────────────────────────────────────

    @staticmethod
    async def fetch_room_id(live_url: str, cookie: str = "") -> str | None:
        """
        从抖音直播间 URL 提取 room_id，支持短链接、分享链接和完整直播间 URL。

        Args:
            live_url: 如 https://live.douyin.com/123456789 或 https://v.douyin.com/xxx
            cookie:   可选浏览器 Cookie

        Returns:
            room_id 字符串，失败返回 None
        """
        headers = dict(_BASE_HEADERS)
        if cookie:
            headers["Cookie"] = cookie

        # ── 先尝试从原始 URL 路径直接提取（快速路径）──────────────────────────
        raw_path = live_url.rstrip("/").split("/")[-1].split("?")[0]
        if raw_path.isdigit():
            return raw_path

        # ── 发起请求，跟随重定向获取最终页面 ─────────────────────────────────
        html = ""
        final_url = live_url
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
                resp = await client.get(live_url, headers=headers)
                html = resp.text
                final_url = str(resp.url)
        except Exception as exc:
            logger.warning("[Douyin] HTTP error fetching URL: %s", exc)

        # ── 从最终 URL 路径提取（跟随重定向后）───────────────────────────────
        final_path = final_url.rstrip("/").split("/")[-1].split("?")[0]
        if final_path.isdigit():
            return final_path

        # ── 从 HTML 中提取（多种模式）────────────────────────────────────────
        for pattern in (_ROOM_URL_RE, _ROOM_DATA_RE, _ROOM_INIT_RE, _ROOM_LIVE_RE):
            m = pattern.search(html)
            if m:
                return m.group(1)

        logger.error("[Douyin] Could not extract room_id from URL: %s (final: %s)", live_url, final_url)
        return None
