# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# 自动启动（推荐，自动处理依赖和 proto 编译）
python3 start.py tiktok @username
python3 start.py douyin 7123456789
python3 start.py douyin https://live.douyin.com/xxx
python3 start.py sessions

# 直接运行（需先激活虚拟环境）
source .venv/bin/activate
python -m src.main tiktok @username
python -m src.main douyin 7123456789 --quiet

# 手动安装依赖
pip install -r requirements.txt

# 编译抖音 Protobuf（仅首次或修改 proto/douyin.proto 后）
pip install grpcio-tools
bash scripts/compile_proto.sh
```

## Architecture

整体采用**生产者-消费者**模型，全程异步（`asyncio`）：

```
Collector（生产者）
    ↓ _emit() → asyncio.Queue（maxsize=10000）
BatchWriter（消费者）
    ↓ 每 BATCH_WRITE_INTERVAL 秒批量 flush
SQLite / PostgreSQL
```

### 关键设计约定

**新增平台采集器**：继承 `src/collectors/base.py:BaseCollector`，只需实现 `async _connect()` 一个方法。所有消息通过 `self._emit(msg_type, user_id, username, content, msg_id)` 推入队列。去重、重连、队列写入均由基类处理。

**Session ID 格式**：`{platform}_{streamer_id}_{unix_timestamp}`，在 `src/main.py` 的 `run_tiktok` / `run_douyin` 函数中构造。

**抖音 Protobuf**：`proto/douyin.proto` 是消息定义源文件，编译产物输出到 `src/proto/douyin_pb2.py`（`.gitignore` 中应排除）。`src/collectors/douyin.py` 使用延迟导入 `try/except` 处理未编译的情况，会以降级模式运行。

**配置**：所有参数通过 `src/config.py:Config` 读取环境变量，`.env` 文件加载自项目根目录。关键参数：`DATABASE_URL`、`DOUYIN_COOKIE`、`BATCH_WRITE_INTERVAL`、`DEDUP_CACHE_SIZE`。

**数据库**：`src/database/db.py` 中的 `engine` 和 `AsyncSessionLocal` 是模块级单例，在进程启动时创建。`init_db()` 每次采集启动时调用（幂等）。`get_session()` 是异步上下文管理器，自动 commit/rollback。

**关闭流程**：`SIGINT`/`SIGTERM` 注册到 event loop，触发 `_shutdown()` → `collector.stop()` → `writer.stop()`（先停采集，再等 BatchWriter 把队列剩余消息全部刷入数据库）→ `end_live_session()`。

### 消息类型（`src/database/models.py:MessageType`）

`chat` | `gift` | `like` | `enter` | `subscribe`

`extra` 字段存储 JSON 格式的平台特有数据（如礼物的 `gift_id`、`repeat_count`）。

## 阶段规划

- **阶段 1（当前）**：弹幕采集 → SQLite
- **阶段 2（待开发）**：直播结束后，全量弹幕 → Claude API → 结构化 Markdown 文档
- **阶段 3（待开发）**：Markdown → ChromaDB 向量索引 → AI 问答（RAG）
- **阶段 4（待开发）**：Web 前端（Next.js + FastAPI）
