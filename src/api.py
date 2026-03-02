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
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
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
    # 服务重启后，DB 里残留的 live 会话实际上已终止，统一标记为 ended
    from sqlalchemy import update
    from datetime import datetime
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(Session)
            .where(Session.status == SessionStatus.LIVE)
            .values(status=SessionStatus.ENDED, ended_at=datetime.utcnow())
        )
        await db.commit()


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


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str) -> JSONResponse:
    if session_id in _active:
        return JSONResponse({"error": "采集中的会话不能删除，请先停止采集"}, status_code=400)
    from sqlalchemy import text
    try:
        async with engine.begin() as conn:
            r1 = await conn.execute(text("DELETE FROM messages WHERE session_id = :sid"), {"sid": session_id})
            r2 = await conn.execute(text("DELETE FROM sessions WHERE id = :sid"), {"sid": session_id})
            logger.info("[delete] session=%s  messages_deleted=%d  sessions_deleted=%d",
                        session_id, r1.rowcount, r2.rowcount)
    except Exception as exc:
        logger.error("[delete] failed: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse({"status": "deleted"})


# ── 设置管理 ──────────────────────────────────────────────────────────────────

SETTINGS_FILE = Path(__file__).parent.parent / "settings.json"


def _load_settings() -> dict:
    if SETTINGS_FILE.exists():
        import json
        try:
            return json.loads(SETTINGS_FILE.read_text())
        except Exception:
            pass
    return {"openrouter_api_key": "", "openrouter_model": "openai/gpt-4o-mini",
            "eulerstream_api_key": "", "douyin_cookie": "",
            "analyze_provider": "deepseek", "analyze_api_key": "", "analyze_model": "deepseek-chat"}


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
    analyze_key   = s.get("analyze_api_key", "")
    return JSONResponse({
        "openrouter_api_key_masked":   _mask_key(or_key),
        "openrouter_api_key_set":      bool(or_key),
        "openrouter_model":            s.get("openrouter_model", "openai/gpt-4o-mini"),
        "eulerstream_api_key_masked":  _mask_key(euler_key),
        "eulerstream_api_key_set":     bool(euler_key),
        "douyin_cookie_masked":        _mask_key(douyin_cookie),
        "douyin_cookie_set":           bool(douyin_cookie),
        "analyze_provider":            s.get("analyze_provider", "deepseek"),
        "analyze_api_key":             analyze_key,
        "analyze_api_key_masked":      _mask_key(analyze_key),
        "analyze_api_key_set":         bool(analyze_key),
        "analyze_model":               s.get("analyze_model", "deepseek-chat"),
    })


@app.post("/api/settings")
async def save_settings(
    openrouter_api_key:  str = Query(default=""),
    openrouter_model:    str = Query(default="openai/gpt-4o-mini"),
    eulerstream_api_key: str = Query(default=""),
    douyin_cookie:       str = Query(default=""),
    analyze_provider:    str = Query(default=""),
    analyze_api_key:     str = Query(default=""),
    analyze_model:       str = Query(default=""),
) -> JSONResponse:
    current = _load_settings()
    if openrouter_api_key:
        current["openrouter_api_key"] = openrouter_api_key.strip()
    if openrouter_model:
        current["openrouter_model"] = openrouter_model.strip()
    if eulerstream_api_key:
        current["eulerstream_api_key"] = eulerstream_api_key.strip()
    if douyin_cookie:
        current["douyin_cookie"] = _normalize_cookie(douyin_cookie)
    if analyze_provider:
        current["analyze_provider"] = analyze_provider.strip()
    if analyze_api_key:
        current["analyze_api_key"] = analyze_api_key.strip()
    if analyze_model:
        current["analyze_model"] = analyze_model.strip()
    _save_settings(current)
    return JSONResponse({"status": "saved"})


# ── AI 分析 ───────────────────────────────────────────────────────────────────

# 所有支持 OpenAI 兼容接口的提供商
_PROVIDERS: dict[str, dict] = {
    "deepseek":   {"name": "DeepSeek",       "base_url": "https://api.deepseek.com/v1",
                   "models": ["deepseek-chat", "deepseek-reasoner"]},
    "qwen":       {"name": "通义千问",        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                   "models": ["qwen-max", "qwen-plus", "qwen-turbo", "qwen-long"]},
    "moonshot":   {"name": "月之暗面 Kimi",   "base_url": "https://api.moonshot.cn/v1",
                   "models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"]},
    "zhipu":      {"name": "智谱 GLM",        "base_url": "https://open.bigmodel.cn/api/paas/v4",
                   "models": ["glm-4-plus", "glm-4-air", "glm-4-flash"]},
    "minimax":    {"name": "MiniMax",         "base_url": "https://api.minimax.chat/v1",
                   "models": ["MiniMax-Text-01", "abab6.5s-chat", "abab6.5-chat"]},
    "openrouter": {"name": "OpenRouter",      "base_url": "https://openrouter.ai/api/v1",
                   "models": ["openai/gpt-4o-mini", "openai/gpt-4o",
                               "anthropic/claude-sonnet-4-6", "google/gemini-flash-1.5",
                               "deepseek/deepseek-chat"],
                   "extra_headers": {"HTTP-Referer": "https://github.com/livescope", "X-Title": "LiveScope"}},
    "openai":     {"name": "OpenAI",          "base_url": "https://api.openai.com/v1",
                   "models": ["gpt-4o-mini", "gpt-4o", "gpt-4-turbo"]},
}

_ANALYSIS_PROMPTS: dict[str, str] = {
    "summary":   "请对以下直播间弹幕数据进行全面总结，包括：主要讨论话题、观众互动热点、整体氛围，给出结构化的分析报告。",
    "sentiment": "请对以下直播间弹幕进行情感分析：正负情感比例、情绪变化趋势、引发强烈反应的话题或时刻，并给出结论。",
    "topics":    "请从以下直播间弹幕中提取主要话题和高频关键词，按热度排序，分析每个话题的讨论背景和观众关注点。",
    "gifts":     "请分析以下直播间的礼物互动数据：主要打赏用户、礼物种类与价值分布、打赏行为与弹幕互动的关联，给出总结。",
    "users":     "请分析以下直播间的用户行为：活跃用户特征、互动规律、核心粉丝与路人观众的区别，总结用户群体画像。",
    "highlight": "请从以下直播间弹幕中找出最精彩、最有趣、最有代表性的弹幕片段，解释为何选择这些内容，并还原当时的互动场景。",
}


@app.get("/api/analyze/providers")
async def get_providers() -> JSONResponse:
    return JSONResponse({pid: {"name": p["name"], "models": p["models"]} for pid, p in _PROVIDERS.items()})


async def _build_analysis_context(session_id: str) -> str | None:
    """将会话数据拼装成结构化文本，作为 AI 提示的上下文。"""
    async with AsyncSessionLocal() as db:
        sess_r = await db.execute(select(Session).where(Session.id == session_id))
        session = sess_r.scalar_one_or_none()
        if not session:
            return None

        counts_r = await db.execute(
            select(Message.msg_type, func.count().label("n"))
            .where(Message.session_id == session_id).group_by(Message.msg_type)
        )
        counts = {r.msg_type: r.n for r in counts_r}

        chat_r = await db.execute(
            select(Message)
            .where(Message.session_id == session_id, Message.msg_type == "chat")
            .order_by(Message.timestamp.asc()).limit(400)
        )
        chats = chat_r.scalars().all()

        gift_r = await db.execute(
            select(Message)
            .where(Message.session_id == session_id, Message.msg_type == "gift")
            .order_by(Message.timestamp.asc()).limit(100)
        )
        gifts = gift_r.scalars().all()

        top_r = await db.execute(
            select(Message.username, func.count().label("n"))
            .where(Message.session_id == session_id, Message.msg_type == "chat")
            .group_by(Message.username).order_by(text("n desc")).limit(10)
        )
        top_users = top_r.all()

    lines: list[str] = []
    lines.append(f"【直播会话信息】")
    lines.append(f"平台：{session.platform}  主播：{session.streamer_id}")
    lines.append(f"开始时间：{str(session.started_at)[:19]}  状态：{session.status}")
    lines.append("")
    lines.append("【互动数据概览】")
    lines.append(f"弹幕 {counts.get('chat', 0)} 条 | 礼物 {counts.get('gift', 0)} 次 | "
                 f"点赞 {counts.get('like', 0)} 次 | 进场 {counts.get('enter', 0)} 次 | "
                 f"关注 {counts.get('subscribe', 0)} 次")
    lines.append("")

    if top_users:
        lines.append("【弹幕活跃用户 Top10】")
        for i, u in enumerate(top_users, 1):
            lines.append(f"{i}. {u.username}（{u.n} 条）")
        lines.append("")

    if gifts:
        import json as _json
        lines.append(f"【礼物列表（共 {len(gifts)} 条）】")
        for g in gifts:
            extra = {}
            try:
                extra = _json.loads(g.extra or "{}")
            except Exception:
                pass
            repeat = extra.get("repeat_count", 1)
            lines.append(f"[{str(g.timestamp)[11:19]}] {g.username} 送出 {g.content} × {repeat}")
        lines.append("")

    if chats:
        lines.append(f"【弹幕内容（共采集 {counts.get('chat', 0)} 条，展示 {len(chats)} 条）】")
        for m in chats:
            lines.append(f"[{str(m.timestamp)[11:19]}] {m.username}：{m.content}")

    return "\n".join(lines)


@app.post("/api/sessions/{session_id}/analyze")
async def analyze_session(session_id: str, body: dict) -> StreamingResponse:
    """
    多轮对话接口。
    body: { provider, api_key, model, messages: [{role, content}, ...] }
    系统提示（含直播数据上下文）由服务端自动注入。
    """
    import json as _json
    import httpx

    provider_id  = body.get("provider", "deepseek")
    api_key      = body.get("api_key", "").strip()
    model        = body.get("model", "").strip()
    messages     = body.get("messages", [])

    if not api_key:
        return JSONResponse({"error": "请先填写 API Key"}, status_code=400)
    if not model:
        return JSONResponse({"error": "请选择或填写模型名称"}, status_code=400)
    if not messages:
        return JSONResponse({"error": "messages 不能为空"}, status_code=400)

    context = await _build_analysis_context(session_id)
    if not context:
        return JSONResponse({"error": "找不到该会话数据"}, status_code=404)

    system_prompt = (
        "你是 LiveScope 直播数据分析助手，专门分析直播间弹幕、礼物、互动数据。"
        "请用中文回答，结果使用 Markdown 格式，结构清晰、重点突出。\n\n"
        f"以下是本场直播的完整数据，请基于此回答用户问题：\n\n{context}"
    )

    prov     = _PROVIDERS.get(provider_id, list(_PROVIDERS.values())[0])
    base_url = prov["base_url"]
    headers  = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
        **prov.get("extra_headers", {}),
    }

    async def _stream():
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                async with client.stream(
                    "POST", f"{base_url}/chat/completions",
                    headers=headers,
                    json={"model": model,
                          "messages": [{"role": "system", "content": system_prompt}] + messages,
                          "stream": True, "temperature": 0.7},
                ) as resp:
                    if resp.status_code != 200:
                        raw = await resp.aread()
                        err_text = raw[:300].decode(errors='replace')
                        yield f"data: {_json.dumps({'error': f'API 错误 {resp.status_code}: {err_text}'})}\n\n"
                        return
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data = line[6:]
                        if data.strip() == "[DONE]":
                            yield "data: [DONE]\n\n"
                            return
                        try:
                            chunk = _json.loads(data)
                            delta = chunk["choices"][0]["delta"].get("content", "")
                            if delta:
                                yield f"data: {_json.dumps({'content': delta})}\n\n"
                        except Exception:
                            pass
        except Exception as exc:
            yield f"data: {_json.dumps({'error': str(exc)})}\n\n"

    return StreamingResponse(_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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
