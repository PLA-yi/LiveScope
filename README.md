# LiveScope — TikTok & 抖音直播 AI 监控工具

> 阶段 1：弹幕实时采集模块

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 编译抖音 Protobuf（仅抖音需要）

```bash
pip install grpcio-tools
bash scripts/compile_proto.sh
```

### 3. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填入抖音 Cookie（可选）
```

### 4. 运行采集

```bash
# TikTok（传主播用户名）
python -m src.main tiktok @username

# 抖音（传 room_id）
python -m src.main douyin 7123456789

# 抖音（传直播间 URL，自动解析 room_id）
python -m src.main douyin https://live.douyin.com/7123456789

# 查看已采集会话
python -m src.main sessions
```

## 项目结构

```
LiveScope/
├── src/
│   ├── config.py              # 配置管理
│   ├── main.py                # CLI 入口
│   ├── database/
│   │   ├── models.py          # Session / Message ORM 模型
│   │   └── db.py              # 异步数据库连接 + BatchWriter
│   ├── collectors/
│   │   ├── base.py            # 基类（去重、重连、emit）
│   │   ├── tiktok.py          # TikTok 采集器
│   │   └── douyin.py          # 抖音采集器
│   ├── processors/
│   │   └── message.py         # 终端实时看板
│   └── proto/                 # Protobuf 编译输出（运行时生成）
├── proto/
│   └── douyin.proto           # 抖音消息 proto 定义
├── scripts/
│   └── compile_proto.sh       # 编译 proto 脚本
├── requirements.txt
└── .env.example
```

## 数据库

默认使用 SQLite（`./livescope.db`），表结构：

| 表 | 说明 |
|---|---|
| `sessions` | 直播会话（平台、主播、开始/结束时间）|
| `messages` | 弹幕消息（类型、用户、内容、时间戳）|

## 获取抖音 Cookie

1. 打开 Chrome，登录抖音网页版
2. 进入任意直播间
3. F12 → Network → 找任意 XHR 请求 → 复制 `Cookie` 请求头
4. 粘贴到 `.env` 的 `DOUYIN_COOKIE` 字段

## 后续阶段计划

- **阶段 2**：直播结束后，将全量弹幕转录为结构化 Markdown 文档
- **阶段 3**：基于 Markdown 建立 ChromaDB 知识库，支持 AI 问答
- **阶段 4**：Web 前端界面
