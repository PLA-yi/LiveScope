from __future__ import annotations

"""
消息处理器：负责从 asyncio.Queue 消费消息并写入数据库。
BatchWriter 已在 db.py 中实现，这里提供统计与监控辅助。
"""

import asyncio
import logging
import time
from collections import defaultdict

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

from src.database.models import Message, MessageType

logger = logging.getLogger(__name__)
console = Console()


class LiveMonitor:
    """
    在终端实时显示弹幕流和统计信息（基于 rich.Live）。
    不影响消息写库，仅用于监控展示。
    """

    def __init__(self, queue: asyncio.Queue[Message], session_label: str = "") -> None:
        self._queue = queue
        self._label = session_label
        self._counts: dict[str, int] = defaultdict(int)
        self._recent: list[tuple[str, str, str]] = []  # (type, username, content)
        self._max_recent = 20
        self._start = time.time()
        self._running = False
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run(), name="live-monitor")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        with Live(self._render(), refresh_per_second=2, console=console) as live:
            while self._running:
                # 非阻塞地读取最新消息更新统计
                try:
                    msg: Message = self._queue.get_nowait()
                    self._update(msg)
                    # 把消息放回去（Monitor 只消费副本，实际写库由 BatchWriter 消费）
                    # 注意：这里不放回，因为 BatchWriter 是独立监听的同一个队列
                    # LiveMonitor 和 BatchWriter 应各读各的队列
                    # → 见 main.py 中的双队列设计
                except asyncio.QueueEmpty:
                    pass
                live.update(self._render())
                await asyncio.sleep(0.5)

    def _update(self, msg: Message) -> None:
        self._counts[msg.msg_type] += 1
        if msg.msg_type == MessageType.CHAT and msg.content:
            self._recent.append((msg.msg_type, msg.username or "?", msg.content))
            if len(self._recent) > self._max_recent:
                self._recent.pop(0)

    def _render(self) -> Table:
        elapsed = int(time.time() - self._start)
        h, m, s = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60

        table = Table(
            title=f"[bold cyan]LiveScope[/] — {self._label}  [{h:02d}:{m:02d}:{s:02d}]",
            show_header=True,
            header_style="bold magenta",
            expand=True,
        )
        table.add_column("时间", style="dim", width=10)
        table.add_column("用户", style="cyan", min_width=12)
        table.add_column("弹幕内容", style="white")

        for _, username, content in self._recent[-15:]:
            import datetime
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            table.add_row(ts, username[:16], content[:60])

        # 底部统计行
        stats = "  ".join(
            f"[yellow]{k}[/]: {v}"
            for k, v in sorted(self._counts.items())
        )
        table.caption = stats or "等待消息……"
        return table
