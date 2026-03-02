from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./livescope.db")
    DOUYIN_COOKIE: str = os.getenv("DOUYIN_COOKIE", "")
    EULERSTREAM_API_KEY: str = os.getenv("EULERSTREAM_API_KEY", "")
    BATCH_WRITE_INTERVAL: float = float(os.getenv("BATCH_WRITE_INTERVAL", "2"))
    DEDUP_CACHE_SIZE: int = int(os.getenv("DEDUP_CACHE_SIZE", "5000"))


config = Config()
