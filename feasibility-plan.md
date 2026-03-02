# TikTok & 抖音直播间 AI 监控工具 — 技术可行性方案

> 版本 v1.0 | 日期：2026-03-02

---

## 一、项目目标概述

| 功能模块 | 描述 |
|---|---|
| 弹幕采集 | 实时抓取 TikTok / 抖音直播间的用户弹幕消息 |
| 内容归档 | 将整场直播的弹幕、主题、关键词转为结构化 Markdown 文档 |
| AI 问答 | 基于直播内容文档，支持自然语言检索与问答 |

---

## 二、技术可行性分析

### 2.1 数据采集层

#### TikTok（国际版）

TikTok 的直播弹幕通过 **WebSocket + Protobuf** 协议传输，目前有开源社区已完成逆向工程。

| 方案 | 库 / 工具 | 可用性 | 备注 |
|---|---|---|---|
| 开源逆向库 | `TikTokLive`（Python） | ✅ 可用 | 无需账号，仅需主播用户名 |
| 官方 API | TikTok Display API | ⚠️ 受限 | 仅授权合作伙伴 |
| 浏览器自动化 | Playwright / Puppeteer | ✅ 备用 | 抗封禁能力更强，资源消耗大 |

```python
# TikTokLive 基本示例
from TikTokLive import TikTokLiveClient
from TikTokLive.events import CommentEvent

client = TikTokLiveClient(unique_id="@主播用户名")

@client.on(CommentEvent)
async def on_comment(event: CommentEvent):
    print(f"[{event.user.nickname}]: {event.comment}")

client.run()
```

#### 抖音（国内版）

抖音直播使用 **WebSocket + Protobuf**，协议与 TikTok 存在差异。

| 方案 | 库 / 工具 | 可用性 | 备注 |
|---|---|---|---|
| 开源逆向库 | `douyin-live`、`dy-barrage-grab` | ✅ 可用 | 需要直播间 room_id |
| Cookie 认证 | 抓包获取 `ttwid` / `__ac_nonce` | ✅ 推荐 | 提升稳定性和反检测 |
| 浏览器自动化 | DrissionPage / Playwright | ✅ 备用 | 自动维护 Cookie |

```python
# 抖音直播 WebSocket 连接示意（简化）
import asyncio, websockets, httpx

async def connect_douyin(room_id: str, cookies: dict):
    ws_url = f"wss://webcast5-ws-web-hl.douyin.com/webcast/im/push/v2/?room_id={room_id}"
    async with websockets.connect(ws_url, extra_headers=cookies) as ws:
        async for message in ws:
            # 解析 Protobuf 消息
            parse_message(message)
```

**关键挑战：**
- 抖音有较强的反爬机制（签名算法 `X-Bogus`、`_signature`）
- 需要定期更新签名逻辑或使用 JS 运行时（如 `execjs`、`quickjs`）

---

### 2.2 数据处理层

```
原始 Protobuf 消息
       ↓
  消息解析器（Python protobuf）
       ↓
  结构化消息对象
  {user, content, timestamp, type}
       ↓
  消息分类器（弹幕/点赞/礼物/进场）
       ↓
  持久化存储（SQLite / PostgreSQL）
```

**消息类型分类：**

| 类型 | 说明 | 是否采集 |
|---|---|---|
| Chat（弹幕） | 用户发送的文字消息 | ✅ 核心 |
| Gift（礼物） | 打赏事件 | ✅ 可选 |
| Like（点赞） | 点赞事件 | ⚪ 统计用 |
| Enter（进场） | 用户进入直播间 | ⚪ 统计用 |
| Subscribe（关注） | 新关注事件 | ⚪ 可选 |

---

### 2.3 AI 处理层

使用 **Anthropic Claude API** 完成以下任务：

#### 实时摘要（Streaming 模式）

每隔 N 分钟，将最新弹幕批次发送给 AI，生成阶段性摘要。

```python
import anthropic

client = anthropic.Anthropic()

def summarize_batch(messages: list[str], interval_minutes: int) -> str:
    prompt = f"""以下是一场直播间过去 {interval_minutes} 分钟的弹幕内容：

{chr(10).join(messages)}

请总结这段时间内：
1. 观众主要在讨论什么话题
2. 情绪倾向（正面/负面/中性）
3. 出现频率最高的关键词（Top 10）
4. 是否有异常互动或重要事件"""

    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text
```

#### 全场总结与 Markdown 生成

直播结束后，聚合所有阶段摘要，生成结构化文档。

```python
def generate_final_report(session_data: dict) -> str:
    """生成完整的直播间分析报告"""
    prompt = f"""
    直播基本信息：{session_data['meta']}
    阶段摘要列表：{session_data['summaries']}
    总弹幕数量：{session_data['total_messages']}

    请生成一份结构化 Markdown 报告，包含：
    # 直播间内容报告
    ## 1. 直播概览
    ## 2. 时间线与主题变化
    ## 3. 热门话题与关键词云
    ## 4. 观众情绪分析
    ## 5. 重要时刻回顾
    ## 6. 原始弹幕样本（每5分钟取样）
    """
    # 调用 Claude API...
```

#### RAG 问答系统

使用向量数据库实现基于直播内容的检索增强生成。

```python
# 推荐技术栈
# 向量数据库：ChromaDB（轻量）或 Qdrant（生产级）
# Embedding：Voyage AI 或 OpenAI text-embedding-3-small
# 框架：可选 LangChain / 手写 RAG

import chromadb
from anthropic import Anthropic

def query_live_content(question: str, session_id: str) -> str:
    # 1. 向量检索相关片段
    results = chroma_collection.query(
        query_texts=[question],
        n_results=5,
        where={"session_id": session_id}
    )
    context = "\n".join(results['documents'][0])

    # 2. 使用 Claude 生成答案
    client = Anthropic()
    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=512,
        system="你是一名直播间内容分析助手，请基于提供的直播弹幕内容回答问题。",
        messages=[{
            "role": "user",
            "content": f"相关直播内容：\n{context}\n\n问题：{question}"
        }]
    )
    return response.content[0].text
```

---

### 2.4 存储层设计

```sql
-- 直播会话表
CREATE TABLE sessions (
    id          TEXT PRIMARY KEY,
    platform    TEXT NOT NULL,       -- 'tiktok' | 'douyin'
    streamer_id TEXT NOT NULL,
    room_id     TEXT,
    title       TEXT,
    started_at  DATETIME,
    ended_at    DATETIME,
    status      TEXT DEFAULT 'live'  -- 'live' | 'ended'
);

-- 弹幕消息表
CREATE TABLE messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    msg_type    TEXT NOT NULL,       -- 'chat' | 'gift' | 'like' | 'enter'
    user_id     TEXT,
    username    TEXT,
    content     TEXT,
    timestamp   DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

-- AI 摘要表
CREATE TABLE summaries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    interval_no INTEGER,             -- 第几个时间段
    started_at  DATETIME,
    ended_at    DATETIME,
    summary     TEXT,                -- AI 生成的摘要
    keywords    TEXT,                -- JSON 格式关键词
    sentiment   TEXT                 -- 'positive' | 'negative' | 'neutral'
);

-- 最终报告表
CREATE TABLE reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT UNIQUE NOT NULL,
    markdown    TEXT,                -- 完整 Markdown 报告
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

---

## 三、整体系统架构

```
┌─────────────────────────────────────────────────────┐
│                   数据采集层                          │
│  ┌──────────────────┐  ┌──────────────────────────┐  │
│  │  TikTok WebSocket│  │  抖音 WebSocket + 签名    │  │
│  │  (TikTokLive)    │  │  (dy-barrage-grab)       │  │
│  └────────┬─────────┘  └────────────┬─────────────┘  │
└───────────┼─────────────────────────┼───────────────┘
            ↓                         ↓
┌─────────────────────────────────────────────────────┐
│                   消息处理层                          │
│   Protobuf 解析 → 消息分类 → 去重过滤 → 队列缓冲      │
└──────────────────────────┬──────────────────────────┘
                           ↓
┌─────────────────┐   ┌────────────────────────────────┐
│   SQLite / PG   │←──│         核心业务服务            │
│   (持久化存储)   │   │   FastAPI / Python asyncio     │
└─────────────────┘   └──────────────┬─────────────────┘
                                     ↓
┌─────────────────────────────────────────────────────┐
│                    AI 处理层                         │
│  ┌─────────────────┐     ┌──────────────────────┐   │
│  │  实时批次摘要    │     │  向量化 + ChromaDB    │   │
│  │  (Claude API)   │     │  (Embedding + RAG)   │   │
│  └────────┬────────┘     └──────────┬───────────┘   │
└───────────┼────────────────────────┼───────────────┘
            ↓                        ↓
┌─────────────────────────────────────────────────────┐
│                   输出层                             │
│   Markdown 报告文件  |  HTML 可视化  |  AI 问答界面   │
└─────────────────────────────────────────────────────┘
```

---

## 四、推荐技术栈

| 层级 | 技术 | 理由 |
|---|---|---|
| 后端语言 | Python 3.11+ | 生态最丰富，asyncio 支持并发 |
| Web 框架 | FastAPI | 异步、高性能、自带 OpenAPI |
| 弹幕采集 | TikTokLive + 自定义抖音客户端 | 成熟方案 |
| 消息队列 | asyncio.Queue 或 Redis Streams | 缓冲高并发弹幕 |
| 数据库 | SQLite（开发）/ PostgreSQL（生产） | 简单易部署 |
| 向量数据库 | ChromaDB | 轻量，纯 Python，无需额外服务 |
| AI 模型 | Claude claude-opus-4-6 (Anthropic) | 长上下文、中英文优秀 |
| Embedding | voyage-3 (Voyage AI) | 性价比高，中文支持好 |
| 前端 | Next.js + Tailwind CSS | 实时 SSE 推送，现代 UI |
| 部署 | Docker Compose | 一键部署所有服务 |

---

## 五、核心挑战与应对方案

### 挑战 1：反爬与封禁

| 风险 | 应对方案 |
|---|---|
| IP 被封 | 使用住宅代理池轮换 |
| 签名算法更新 | 监控开源社区，设置自动告警 |
| Cookie 失效 | 定时刷新 Cookie，多账号备份 |
| WebSocket 断连 | 自动重连机制，指数退避 |

### 挑战 2：高并发弹幕

热门直播每秒可达 **数百条弹幕**，需要：
- 异步消息队列缓冲（asyncio.Queue 或 Redis Streams）
- 批量写入数据库（每 500ms 批量 INSERT）
- 弹幕去重（基于消息 ID 的 LRU 缓存）

### 挑战 3：AI 成本控制

| 策略 | 效果 |
|---|---|
| 批量处理（每5分钟一批） | 减少 API 调用次数 80% |
| 弹幕去重 + 过滤无效消息 | 减少 Token 消耗 30-50% |
| 使用 claude-haiku-4-5 做初筛 | 降低成本，复杂任务再用 Opus |
| 本地 Embedding 模型 | 向量化完全免费 |

---

## 六、开发里程碑

```
阶段 1（1-2周）：基础采集
  ✓ TikTok 弹幕采集模块
  ✓ 抖音弹幕采集模块
  ✓ SQLite 数据持久化

阶段 2（2-3周）：AI 处理
  ✓ Claude API 集成
  ✓ 实时分批摘要
  ✓ Markdown 报告生成

阶段 3（1-2周）：RAG 问答
  ✓ 向量化索引构建
  ✓ ChromaDB 集成
  ✓ 问答接口开发

阶段 4（1-2周）：前端界面
  ✓ 实时弹幕看板
  ✓ 报告查看器
  ✓ AI 问答 Chat UI

阶段 5（1周）：部署优化
  ✓ Docker 容器化
  ✓ 监控与告警
  ✓ 文档完善
```

---

## 七、合规与风险声明

> ⚠️ **重要提示**

1. **平台服务条款**：TikTok 和抖音的服务条款均禁止未授权爬取数据，使用本工具需自行评估法律风险。
2. **数据隐私**：弹幕内容涉及用户个人信息，存储和使用需符合所在地区隐私法规（如 GDPR、中国个人信息保护法）。
3. **商业使用**：如用于商业目的，建议通过官方合作渠道获取数据授权。
4. **仅供学习研究**：本方案旨在技术探索，推荐用于个人学习、学术研究等非商业场景。

---

*文档生成时间：2026-03-02 | 技术方案版本：v1.0*
