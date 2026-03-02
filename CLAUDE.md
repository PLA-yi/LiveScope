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

**外部运行时依赖**：系统需安装 Node.js（供 PyExecJS 调用 `sign.js` 计算抖音签名）。

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
    ↑  轮询（2.5s interval, since_id 增量）
前端（frontend/index.html，单文件 SPA）
```

## 抖音采集器关键实现

**无需 Cookie，全自动连接流程**（`src/collectors/douyin.py`）：
1. GET `https://live.douyin.com/` → 自动提取 `ttwid` cookie
2. GET `https://live.douyin.com/{web_rid}` → 正则提取真实 `room_id`（`roomId\\":\\"(\d+)\\"`）
3. 构建 WSS URL，调用 `src/douyin_sign/sign.js`（execjs + Node.js）计算 `signature` 参数
4. WebSocket 连接到 `webcast100-ws-web-lq.douyin.com`
5. 每帧解析：`PushFrame` → 检测 gzip 魔术字节 `\x1f\x8b` 解压（不依赖 `payloadEncoding` 字段）→ `Response` → 分发消息

**签名文件**：`src/douyin_sign/sign.js`、`a_bogus.js`（来自 DouyinLiveWebFetcher），由 `start.py` 自动下载。

**Proto 字段命名**：`grpc_tools.protoc` 生成的字段保留 camelCase（如 `messagesList`、`needAck`、`payloadEncoding`），与标准 proto3 snake_case 不同，避免用 `messages_list` 等形式访问。

## 新增采集器

继承 `src/collectors/base.py:BaseCollector`，只需实现一个方法：

```python
async def _connect(self) -> None:
    # 建立连接，持续接收消息直到 self._running=False
    # 消息通过 self._emit(msg_type, user_id, username, content, msg_id) 推入队列
```

基类自动处理：LRU 去重（按 `msg_id`，5000条上限）、指数退避重连（最多 10 次，上限 120s）、队列写入。

## 关键配置（src/config.py）

| 环境变量 | 说明 |
|---|---|
| `DATABASE_URL` | 默认 `sqlite+aiosqlite:///./livescope.db` |
| `DOUYIN_COOKIE` | 可选，Cookie 字符串（现已可自动获取 ttwid） |
| `EULERSTREAM_API_KEY` | TikTok 采集所需的签名服务 Key |
| `BATCH_WRITE_INTERVAL` | BatchWriter 刷新间隔，默认 2 秒 |
| `DEDUP_CACHE_SIZE` | 去重 LRU 缓存大小，默认 5000 |

`settings.json`（项目根目录）中的值优先于 `.env`，由 Web 看板设置页写入。`src/api.py` 的 `_load_settings()` 在每次请求时读取文件，无需重启。

## Web API（src/api.py）

| 端点 | 说明 |
|---|---|
| `POST /api/collect/start?platform=douyin&target=96089858297` | 启动采集 |
| `POST /api/collect/stop?session_id=...` | 停止采集 |
| `DELETE /api/sessions/{id}` | 删除会话及其所有消息 |
| `GET /api/sessions` | 会话列表（最近 50 条） |
| `GET /api/sessions/{id}/messages?type=chat&since_id=0` | 消息分页（支持增量轮询） |
| `GET /api/sessions/{id}/stats` | 统计（弹幕数、top 用户、时间线） |
| `POST /api/sessions/{id}/analyze` | AI 流式分析（SSE，body: `{provider, api_key, model, messages}`） |
| `GET /api/analyze/providers` | 返回支持的 AI provider 及模型列表 |
| `POST /api/translate` | 批量翻译（OpenRouter API，body: `{texts: [...]}`） |
| `GET/POST /api/settings` | 读写 settings.json |

活动采集任务存储在模块级 `_active` 字典（`session_id → {task, writer, collector}`），进程重启后丢失。**服务启动时会将数据库中所有残留的 `status=live` 会话自动标记为 `ended`。**

## AI 分析功能（src/api.py）

`_PROVIDERS` 字典定义了所有支持 OpenAI 兼容接口的提供商（DeepSeek、Qwen、Moonshot、智谱、MiniMax、OpenRouter、OpenAI），通过 `httpx` 流式转发 SSE。

`_build_analysis_context()` 将会话的弹幕/礼物/统计数据组装成结构化文本注入 system prompt，作为多轮对话上下文（取 400 条弹幕 + 100 条礼物）。

## 数据库操作规范

`Session` model 上有 `relationship("Message", lazy="dynamic")`，ORM 层的 bulk delete 会因 session 缓存产生冲突。**所有 DELETE 操作必须用 `engine.begin()` + raw SQL（`sqlalchemy.text`）**，不要用 ORM session：

```python
from sqlalchemy import text
from src.database.db import engine

async with engine.begin() as conn:
    await conn.execute(text("DELETE FROM messages WHERE session_id = :sid"), {"sid": session_id})
    await conn.execute(text("DELETE FROM sessions WHERE id = :sid"), {"sid": session_id})
# engine.begin() 自动 commit，无需手动调用
```

查询操作继续用 `AsyncSessionLocal` + SQLAlchemy ORM（`select()`）。

**注意**：`api.py` 的 `delete_session` 端点使用了 `engine`，但当前未在文件顶部导入，需确保 `from src.database.db import engine` 存在。

## 消息类型（src/database/models.py）

`chat` | `gift` | `like` | `enter` | `subscribe`

`extra` 字段存 JSON（礼物消息含 `gift_id`、`repeat_count`）。

## 前端（frontend/index.html）

单文件 SPA，无构建步骤，直接编辑即可。关键注意事项：

- **会话列表渲染只有一处**：`loadSessions()` 函数。页面初始化、删除、停止、刷新后都调用此函数，**不要在其他地方重复写渲染逻辑**，否则新增的 UI 元素（如删除按钮）会在初始化时缺失。
- **初始化入口**：文件末尾的 IIFE `(async()=>{ await loadSessions(); ... })()`，调用 `loadSessions()` 后再选中首条会话。
- **onclick 中传对象**：用 `JSON.stringify(obj).replace(/'/g,"&#39;")` 转义单引号，避免 HTML 属性中断。
- **阻止冒泡**：会话 item 子元素的按钮（如删除按钮）需要 `event.stopPropagation()`，防止触发父元素的 `selectSession`。
- **AI 分析面板**：左侧配置栏（provider/model/api_key）+ 右侧对话区，通过 `EventSource`/`fetch` 消费 SSE 流，Markdown 由前端简易解析器渲染。

## 阶段规划

- **阶段 1（当前）**：弹幕采集 → SQLite + Web 看板
- **阶段 2（待开发）**：直播结束后全量弹幕 → Claude API → 结构化 Markdown
- **阶段 3（待开发）**：Markdown → ChromaDB 向量索引 → AI 问答（RAG）
