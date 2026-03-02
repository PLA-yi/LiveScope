from __future__ import annotations

"""
LiveScope Web API  —  REST 接口 + 后台采集任务管理
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import func, select, text

from src.config import config
from src.database.db import AsyncSessionLocal, BatchWriter, end_live_session, init_db, upsert_live_session
from src.database.models import Message, MessageType, Platform, Session, SessionStatus

logger = logging.getLogger(__name__)

app = FastAPI(title="LiveScope API", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

# ── 全局：正在运行的采集任务 ─────────────────────────────────────────────────
# { session_id: {"task": asyncio.Task, "writer": BatchWriter, "collector": BaseCollector} }
_active: dict[str, dict] = {}


@app.on_event("startup")
async def startup() -> None:
    await init_db()


# ── 静态首页 ──────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


# ── Sessions ─────────────────────────────────────────────────────────────────

@app.get("/api/sessions")
async def list_sessions() -> JSONResponse:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Session).order_by(Session.started_at.desc()).limit(50)
        )
        sessions = result.scalars().all()

    data = []
    for s in sessions:
        live = s.id in _active
        data.append({
            "id":          s.id,
            "platform":    s.platform,
            "streamer_id": s.streamer_id,
            "room_id":     s.room_id,
            "status":      "live" if live else s.status,
            "started_at":  str(s.started_at)[:19] if s.started_at else None,
            "ended_at":    str(s.ended_at)[:19]   if s.ended_at   else None,
        })
    return JSONResponse(data)


@app.get("/api/sessions/{session_id}/stats")
async def get_stats(session_id: str) -> JSONResponse:
    async with AsyncSessionLocal() as db:
        counts_r = await db.execute(
            select(Message.msg_type, func.count().label("n"))
            .where(Message.session_id == session_id)
            .group_by(Message.msg_type)
        )
        counts = {row.msg_type: row.n for row in counts_r}

        top_r = await db.execute(
            select(Message.username, Message.user_id, func.count().label("n"))
            .where(Message.session_id == session_id, Message.msg_type == "chat")
            .group_by(Message.username, Message.user_id)
            .order_by(text("n desc"))
            .limit(10)
        )
        top_users = [{"username": r.username, "user_id": r.user_id, "count": r.n} for r in top_r]

        timeline_r = await db.execute(
            select(
                func.strftime("%H:%M", Message.timestamp).label("minute"),
                func.count().label("n"),
            )
            .where(Message.session_id == session_id, Message.msg_type == "chat")
            .group_by(text("minute"))
            .order_by(text("minute"))
        )
        timeline = [{"minute": r.minute, "count": r.n} for r in timeline_r]

    return JSONResponse({
        "counts":    counts,
        "total":     sum(counts.values()),
        "top_users": top_users,
        "timeline":  timeline,
    })


@app.get("/api/sessions/{session_id}/messages")
async def get_messages(
    session_id: str,
    msg_type:   str           = Query("chat", alias="type"),
    limit:      int           = Query(200, le=1000),
    offset:     int           = Query(0),
    since_id:   Optional[int] = Query(None),
) -> JSONResponse:
    async with AsyncSessionLocal() as db:
        q = select(Message).where(Message.session_id == session_id)
        if msg_type != "all":
            q = q.where(Message.msg_type == msg_type)
        if since_id is not None:
            q = q.where(Message.id > since_id)
        q = q.order_by(Message.timestamp.asc()).offset(offset).limit(limit)
        result = await db.execute(q)
        rows = result.scalars().all()

    data = [{
        "id":        m.id,
        "msg_type":  m.msg_type,
        "user_id":   m.user_id,
        "username":  m.username,
        "content":   m.content,
        "extra":     m.extra,
        "timestamp": str(m.timestamp)[11:19] if m.timestamp else "",
    } for m in rows]
    return JSONResponse(data)


# ── 采集管理 ──────────────────────────────────────────────────────────────────

@app.get("/api/collect/status")
async def collect_status() -> JSONResponse:
    return JSONResponse({
        "active_sessions": list(_active.keys()),
        "count": len(_active),
    })


@app.post("/api/collect/start")
async def start_collect(
    platform: str = Query(..., description="tiktok 或 douyin"),
    target:   str = Query(..., description="TikTok: @username；抖音: room_id 或 URL"),
    ws_url:   str = Query(default="", description="（抖音）直接指定完整 wss:// 地址"),
) -> JSONResponse:
    platform = platform.lower()

    if platform == "tiktok":
        unique_id = target if target.startswith("@") else f"@{target}"
        room_id   = None
        web_rid   = None
        streamer  = unique_id
    elif platform == "douyin":
        if target.startswith("http"):
            from src.collectors.douyin import DouyinCollector
            web_rid = await DouyinCollector.fetch_room_id(target, config.DOUYIN_COOKIE)
            if not web_rid:
                return JSONResponse({"error": "无法从 URL 解析 room_id"}, status_code=400)
        else:
            web_rid = target
        room_id  = web_rid   # 实际 room_id 在采集器连接时通过 API 解析
        streamer = web_rid
    else:
        return JSONResponse({"error": "platform 只能是 tiktok 或 douyin"}, status_code=400)

    session_id = f"{platform}_{streamer.lstrip('@')}_{int(datetime.utcnow().timestamp())}"
    session = Session(
        id=session_id,
        platform=platform,
        streamer_id=streamer,
        room_id=room_id,
        started_at=datetime.utcnow(),
        status=SessionStatus.LIVE,
    )
    await upsert_live_session(session)

    write_q: asyncio.Queue = asyncio.Queue(maxsize=10000)
    writer = BatchWriter(queue=write_q, interval=config.BATCH_WRITE_INTERVAL)
    writer.start()

    if platform == "tiktok":
        from src.collectors.tiktok import TikTokCollector
        # settings.json 中的 Key 优先于 .env
        euler_key = _load_settings().get("eulerstream_api_key", "") or config.EULERSTREAM_API_KEY
        collector = TikTokCollector(session=session, queue=write_q, unique_id=streamer, euler_key=euler_key)
    else:
        from src.collectors.douyin import DouyinCollector
        # settings.json 中的 Cookie 优先于 .env
        settings = _load_settings()
        douyin_cookie = settings.get("douyin_cookie", "") or config.DOUYIN_COOKIE
        collector = DouyinCollector(
            session=session, queue=write_q,
            room_id=room_id, cookie=douyin_cookie,
            web_rid=web_rid, ws_url=ws_url,
        )

    task = asyncio.create_task(collector.start(), name=f"collect-{session_id}")

    _active[session_id] = {
        "task":      task,
        "writer":    writer,
        "collector": collector,
        "session":   session,
    }

    # 任务结束时自动清理
    def _on_done(t: asyncio.Task) -> None:
        _active.pop(session_id, None)
        asyncio.create_task(_cleanup(session_id, writer))

    task.add_done_callback(_on_done)

    logger.info("Collection started: %s", session_id)
    return JSONResponse({"session_id": session_id, "status": "started"})


async def _cleanup(session_id: str, writer: BatchWriter) -> None:
    await writer.stop()
    await end_live_session(session_id)


@app.post("/api/collect/stop")
async def stop_collect(session_id: str = Query(...)) -> JSONResponse:
    entry = _active.get(session_id)
    if not entry:
        return JSONResponse({"error": "该会话未在采集中"}, status_code=404)

    await entry["collector"].stop()
    entry["task"].cancel()
    return JSONResponse({"session_id": session_id, "status": "stopped"})


# ── 设置管理 ──────────────────────────────────────────────────────────────────

SETTINGS_FILE = Path(__file__).parent.parent / "settings.json"


def _load_settings() -> dict:
    if SETTINGS_FILE.exists():
        import json
        try:
            return json.loads(SETTINGS_FILE.read_text())
        except Exception:
            pass
    return {"openrouter_api_key": "", "openrouter_model": "openai/gpt-4o-mini", "eulerstream_api_key": "", "douyin_cookie": ""}


def _save_settings(data: dict) -> None:
    import json
    SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def _mask_key(key: str) -> str:
    return key[:8] + "…" if len(key) > 8 else ("(未设置)" if not key else key)


def _normalize_cookie(raw: str) -> str:
    """
    兼容两种 Cookie 格式：
      1. Header 字符串：name=value; name2=value2
      2. JSON 数组：[{"name": "...", "value": "..."}, ...]
    统一转换为 Header 字符串格式。
    """
    import json as _json
    raw = raw.strip()
    if raw.startswith("["):
        try:
            items = _json.loads(raw)
            return "; ".join(f"{c['name']}={c['value']}" for c in items if "name" in c)
        except Exception:
            pass
    return raw


@app.get("/api/settings")
async def get_settings() -> JSONResponse:
    s = _load_settings()
    or_key        = s.get("openrouter_api_key", "")
    euler_key     = s.get("eulerstream_api_key", "")
    douyin_cookie = s.get("douyin_cookie", "")
    return JSONResponse({
        "openrouter_api_key_masked":   _mask_key(or_key),
        "openrouter_api_key_set":      bool(or_key),
        "openrouter_model":            s.get("openrouter_model", "openai/gpt-4o-mini"),
        "eulerstream_api_key_masked":  _mask_key(euler_key),
        "eulerstream_api_key_set":     bool(euler_key),
        "douyin_cookie_masked":        _mask_key(douyin_cookie),
        "douyin_cookie_set":           bool(douyin_cookie),
    })


@app.post("/api/settings")
async def save_settings(
    openrouter_api_key:  str = Query(default=""),
    openrouter_model:    str = Query(default="openai/gpt-4o-mini"),
    eulerstream_api_key: str = Query(default=""),
    douyin_cookie:       str = Query(default=""),
) -> JSONResponse:
    current = _load_settings()
    # 空值表示"不修改"（前端隐码展示时用户不填）
    if openrouter_api_key:
        current["openrouter_api_key"] = openrouter_api_key.strip()
    if openrouter_model:
        current["openrouter_model"] = openrouter_model.strip()
    if eulerstream_api_key:
        current["eulerstream_api_key"] = eulerstream_api_key.strip()
    if douyin_cookie:
        current["douyin_cookie"] = _normalize_cookie(douyin_cookie)
    _save_settings(current)
    return JSONResponse({"status": "saved"})


# ── 翻译 ──────────────────────────────────────────────────────────────────────

@app.post("/api/translate")
async def translate(body: dict) -> JSONResponse:
    """
    body: { "texts": ["hello", "world", ...] }
    返回: { "translations": ["你好", "世界", ...] }
    """
    import httpx, json as _json

    texts: list[str] = body.get("texts", [])
    if not texts:
        return JSONResponse({"translations": []})

    settings = _load_settings()
    api_key  = settings.get("openrouter_api_key", "")
    model    = settings.get("openrouter_model", "openai/gpt-4o-mini")

    if not api_key:
        return JSONResponse({"error": "未配置 OpenRouter API Key"}, status_code=400)

    # 批量翻译：把所有文本编号后放入一条消息
    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(texts))
    prompt = (
        "将以下编号文本逐条翻译为简体中文。"
        "仅返回 JSON 数组，每个元素对应同序号的翻译结果，不加解释。\n\n"
        f"{numbered}"
    )

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type":  "application/json",
                    "HTTP-Referer":  "https://github.com/livescope",
                    "X-Title":       "LiveScope",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1,
                },
            )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()

        # 解析 JSON 数组（模型有时会包裹在 ```json ``` 中）
        if "```" in content:
            content = content.split("```")[1].lstrip("json").strip()
        translations: list[str] = _json.loads(content)

        # 长度对齐保护
        while len(translations) < len(texts):
            translations.append("")

        return JSONResponse({"translations": translations[:len(texts)]})

    except httpx.HTTPStatusError as e:
        logger.error("OpenRouter HTTP error: %s %s", e.response.status_code, e.response.text[:200])
        return JSONResponse({"error": f"API 错误 {e.response.status_code}"}, status_code=502)
    except Exception as e:
        logger.error("translate error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)
