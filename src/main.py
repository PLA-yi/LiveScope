from __future__ import annotations

"""
LiveScope — 阶段 1：弹幕采集入口

用法：
    # TikTok
    python -m src.main tiktok @username

    # 抖音（传 room_id）
    python -m src.main douyin 7123456789

    # 抖音（传直播间 URL，自动解析 room_id）
    python -m src.main douyin https://live.douyin.com/7123456789

    # 静默模式（不显示实时看板，只写库）
    python -m src.main tiktok @username --quiet
"""

import asyncio
import logging
import signal
import sys
import uuid
from datetime import datetime
from pathlib import Path

import click
from rich.logging import RichHandler

from src.config import config
from src.database.db import BatchWriter, end_live_session, init_db, upsert_live_session
from src.database.models import Platform, Session, SessionStatus

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
)
logger = logging.getLogger(__name__)


# ── 采集主逻辑 ────────────────────────────────────────────────────────────────

async def run_tiktok(unique_id: str, quiet: bool) -> None:
    from src.collectors.tiktok import TikTokCollector

    # 规范化用户名
    if not unique_id.startswith("@"):
        unique_id = f"@{unique_id}"

    session_id = f"tiktok_{unique_id.lstrip('@')}_{int(datetime.utcnow().timestamp())}"
    session = Session(
        id=session_id,
        platform=Platform.TIKTOK,
        streamer_id=unique_id,
        started_at=datetime.utcnow(),
        status=SessionStatus.LIVE,
    )

    await init_db()
    await upsert_live_session(session)

    # 双队列：采集器 → write_q（BatchWriter消费）& monitor_q（LiveMonitor消费）
    write_q: asyncio.Queue = asyncio.Queue(maxsize=10000)

    collector = TikTokCollector(session=session, queue=write_q, unique_id=unique_id)
    writer = BatchWriter(queue=write_q, interval=config.BATCH_WRITE_INTERVAL)

    writer.start()

    logger.info("Starting TikTok collection for %s (session=%s)", unique_id, session_id)

    async def _shutdown() -> None:
        logger.info("Shutting down …")
        await collector.stop()
        await writer.stop()
        await end_live_session(session_id)
        logger.info("Session %s ended.", session_id)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown()))

    try:
        await collector.start()
    finally:
        await _shutdown()


async def run_douyin(target: str, quiet: bool) -> None:
    from src.collectors.douyin import DouyinCollector

    cookie = config.DOUYIN_COOKIE

    # 如果传入的是 URL，先提取 room_id
    if target.startswith("http"):
        logger.info("Fetching room_id from URL …")
        room_id = await DouyinCollector.fetch_room_id(target, cookie)
        if not room_id:
            logger.error("Failed to extract room_id, aborting.")
            return
    else:
        room_id = target

    session_id = f"douyin_{room_id}_{int(datetime.utcnow().timestamp())}"
    session = Session(
        id=session_id,
        platform=Platform.DOUYIN,
        streamer_id=room_id,
        room_id=room_id,
        started_at=datetime.utcnow(),
        status=SessionStatus.LIVE,
    )

    await init_db()
    await upsert_live_session(session)

    write_q: asyncio.Queue = asyncio.Queue(maxsize=10000)

    collector = DouyinCollector(session=session, queue=write_q, room_id=room_id, cookie=cookie)
    writer = BatchWriter(queue=write_q, interval=config.BATCH_WRITE_INTERVAL)

    writer.start()

    logger.info("Starting Douyin collection for room_id=%s (session=%s)", room_id, session_id)

    async def _shutdown() -> None:
        logger.info("Shutting down …")
        await collector.stop()
        await writer.stop()
        await end_live_session(session_id)
        logger.info("Session %s ended.", session_id)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(_shutdown()))

    try:
        await collector.start()
    finally:
        await _shutdown()


# ── CLI ───────────────────────────────────────────────────────────────────────

@click.group()
def cli() -> None:
    """LiveScope — TikTok & 抖音直播弹幕采集工具"""


@cli.command()
@click.argument("unique_id")
@click.option("--quiet", is_flag=True, default=False, help="不显示实时看板")
def tiktok(unique_id: str, quiet: bool) -> None:
    """采集 TikTok 直播间弹幕。UNIQUE_ID 为主播用户名（如 @username）"""
    asyncio.run(run_tiktok(unique_id, quiet))


@cli.command()
@click.argument("target")
@click.option("--quiet", is_flag=True, default=False, help="不显示实时看板")
def douyin(target: str, quiet: bool) -> None:
    """采集抖音直播间弹幕。TARGET 可为 room_id 或直播间完整 URL"""
    asyncio.run(run_douyin(target, quiet))


@cli.command()
def sessions() -> None:
    """列出所有已采集的直播会话"""

    async def _list() -> None:
        from sqlalchemy.ext.asyncio import AsyncSession
        from sqlalchemy import select
        from src.database.db import AsyncSessionLocal
        from src.database.models import Session as Sess
        from rich.table import Table
        from rich.console import Console

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Sess).order_by(Sess.started_at.desc()).limit(20)
            )
            rows = result.scalars().all()

        table = Table(title="近期直播会话", show_lines=True)
        table.add_column("ID", style="dim", overflow="fold")
        table.add_column("平台", style="cyan")
        table.add_column("主播", style="green")
        table.add_column("状态")
        table.add_column("开始时间")

        for r in rows:
            status_style = "green" if r.status == "live" else "dim"
            table.add_row(
                r.id,
                r.platform,
                r.streamer_id,
                f"[{status_style}]{r.status}[/]",
                str(r.started_at)[:19] if r.started_at else "-",
            )

        Console().print(table)

    asyncio.run(_list())


@cli.command()
@click.argument("session_id", required=False)
@click.option("--type", "msg_type", default="chat", show_default=True,
              help="消息类型：chat / gift / like / enter / subscribe / all")
@click.option("--limit", default=100, show_default=True, help="显示条数")
@click.option("--export", "export_path", default="", help="导出为 .txt 文件路径")
def messages(session_id: str, msg_type: str, limit: int, export_path: str) -> None:
    """查看某场直播的弹幕记录。不传 SESSION_ID 则自动选最近一场。"""

    async def _show() -> None:
        from sqlalchemy import select, func
        from rich.table import Table
        from rich.console import Console
        from rich.panel import Panel
        from src.database.db import AsyncSessionLocal, init_db
        from src.database.models import Session as Sess, Message

        await init_db()

        async with AsyncSessionLocal() as db:
            # 未传 session_id → 取最近一场
            sid = session_id
            if not sid:
                r = await db.execute(
                    select(Sess).order_by(Sess.started_at.desc()).limit(1)
                )
                sess = r.scalar_one_or_none()
                if not sess:
                    Console().print("[red]数据库中暂无任何会话，请先采集一场直播。[/]")
                    return
                sid = sess.id

            # 查询会话信息
            sess_r = await db.execute(select(Sess).where(Sess.id == sid))
            sess = sess_r.scalar_one_or_none()
            if not sess:
                Console().print(f"[red]找不到会话 {sid}[/]")
                return

            # 查询消息
            q = select(Message).where(Message.session_id == sid)
            if msg_type != "all":
                q = q.where(Message.msg_type == msg_type)
            q = q.order_by(Message.timestamp.asc()).limit(limit)
            msg_r = await db.execute(q)
            rows = msg_r.scalars().all()

            # 统计
            total_r = await db.execute(
                select(func.count()).where(Message.session_id == sid)
            )
            total = total_r.scalar()

            chat_r = await db.execute(
                select(func.count()).where(
                    Message.session_id == sid,
                    Message.msg_type == "chat",
                )
            )
            chat_total = chat_r.scalar()

        c = Console()
        c.print(Panel(
            f"[bold]{sess.platform.upper()}[/]  [cyan]{sess.streamer_id}[/]\n"
            f"会话 ID：[dim]{sid}[/]\n"
            f"开始时间：{str(sess.started_at)[:19]}    状态：{'🔴 直播中' if sess.status == 'live' else '⚫ 已结束'}\n"
            f"共记录消息：[yellow]{total}[/] 条（弹幕 {chat_total} 条）",
            title="直播会话信息",
        ))

        if not rows:
            c.print(f"[dim]没有找到类型为 '{msg_type}' 的消息。[/]")
            return

        table = Table(
            title=f"弹幕记录（类型={msg_type}，共 {len(rows)} 条）",
            show_lines=False,
            expand=True,
        )
        table.add_column("时间", style="dim", width=10, no_wrap=True)
        table.add_column("用户", style="cyan", width=18, no_wrap=True)
        table.add_column("内容", style="white")

        lines = []
        for m in rows:
            ts = str(m.timestamp)[11:19] if m.timestamp else ""
            user = (m.username or "")[:18]
            content = m.content or ""
            table.add_row(ts, user, content)
            lines.append(f"[{ts}] {user}: {content}")

        c.print(table)

        if export_path:
            Path(export_path).write_text("\n".join(lines), encoding="utf-8")
            c.print(f"\n[green]已导出到 {export_path}[/]")

    asyncio.run(_show())


if __name__ == "__main__":
    cli()
