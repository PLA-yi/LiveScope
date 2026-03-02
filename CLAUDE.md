# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 启动与运行

```bash
# 一键启动（推荐）：自动安装依赖、编译 proto、下载签名文件、启动 Web 看板
python start.py

# 手动启动 Web 看板（需先激活虚拟环境）
source .venv/bin/activate
uvicorn src.api:app --host 127.0.0.1 --port 8765

# 重新编译 proto（修改 proto/douyin.proto 后执行）
.venv/bin/python -m grpc_tools.protoc -I./proto --python_out=./src/proto ./proto/douyin.proto
```

## 系统架构

**生产者-消费者异步管道**（全程 asyncio）：

```
DouyinCollector / TikTokCollector
    ↓  _emit()
asyncio.Queue（maxsize=10000）
    ↓  每 BATCH_WRITE_INTERVAL 秒
BatchWriter（src/database/db.py）
    ↓  bulk insert
SQLite（livescope.db）
    ↑  REST API 查询
FastAPI（src/api.py）
    ↑  轮询
前端（frontend/index.html，单文件 SPA）
```

## 抖音采集器关键实现

**无需 Cookie，全自动连接流程**（`src/collectors/douyin.py`）：
1. GET `https://live.douyin.com/` → 自动提取 `ttwid` cookie
2. GET `https://live.douyin.com/{web_rid}` → 正则提取真实 `room_id`（`roomId\\":\\"(\d+)\\"`）
3. 构建 WSS URL，调用 `src/douyin_sign/sign.js`（execjs + Node.js）计算 `signature` 参数
4. WebSocket 连接到 `webcast100-ws-web-lq.douyin.com`
5. 每帧解析：`PushFrame` → 检测 gzip 魔术字节 `\x1f\x8b` 解压（不依赖 `payloadEncoding` 字段）→ `Response` → 分发消息

**签名文件**：`src/douyin_sign/sign.js`、`a_bogus.js`（来自 DouyinLiveWebFetcher），由 `start.py` 自动下载。需要系统安装 Node.js。

**Proto 字段命名**：`grpc_tools.protoc` 生成的字段保留 camelCase（如 `messagesList`、`needAck`、`payloadEncoding`），与标准 proto3 snake_case 不同，避免用 `messages_list` 等形式访问。

## 新增采集器

继承 `src/collectors/base.py:BaseCollector`，只需实现一个方法：

```python
async def _connect(self) -> None:
    # 建立连接，持续接收消息直到 self._running=False
    # 消息通过 self._emit(msg_type, user_id, username, content, msg_id) 推入队列
```

基类自动处理：LRU 去重（按 `msg_id`）、指数退避重连（最多 10 次，上限 120s）、队列写入。

## 关键配置（src/config.py）

| 环境变量 | 说明 |
|---|---|
| `DATABASE_URL` | 默认 `sqlite+aiosqlite:///./livescope.db` |
| `DOUYIN_COOKIE` | 可选，Cookie 字符串（现已可自动获取 ttwid） |
| `EULERSTREAM_API_KEY` | TikTok 采集所需的签名服务 Key |
| `BATCH_WRITE_INTERVAL` | BatchWriter 刷新间隔，默认 2 秒 |
| `DEDUP_CACHE_SIZE` | 去重 LRU 缓存大小，默认 10000 |

`settings.json`（项目根目录）中的值优先于 `.env`，由 Web 看板设置页写入。

## Web API（src/api.py）

| 端点 | 说明 |
|---|---|
| `POST /api/collect/start?platform=douyin&target=96089858297` | 启动采集 |
| `POST /api/collect/stop?session_id=...` | 停止采集 |
| `GET /api/sessions` | 会话列表（最近 50 条） |
| `GET /api/sessions/{id}/messages?type=chat&since_id=0` | 消息分页（支持增量轮询） |
| `GET /api/sessions/{id}/stats` | 统计（弹幕数、top 用户、时间线） |
| `POST /api/translate` | 批量翻译（OpenRouter API） |
| `GET/POST /api/settings` | 读写 settings.json |

活动采集任务存储在模块级 `_active` 字典（`session_id → {task, writer, collector}`），进程重启后丢失。

## 消息类型（src/database/models.py）

`chat` | `gift` | `like` | `enter` | `subscribe`

`extra` 字段存 JSON（礼物消息含 `gift_id`、`repeat_count`）。

## 阶段规划

- **阶段 1（当前）**：弹幕采集 → SQLite + Web 看板
- **阶段 2（待开发）**：直播结束后全量弹幕 → Claude API → 结构化 Markdown
- **阶段 3（待开发）**：Markdown → ChromaDB 向量索引 → AI 问答（RAG）
