from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./scheduler.db"
    api_url: str = "http://127.0.0.1:1000"
    timezone: str = "Asia/Shanghai"
    host: str = "127.0.0.1"
    port: int = 1000
    max_workers: int = 10
    default_timeout: int = 300

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=os.getenv("SCHEDULER_DATABASE_URL", cls.database_url),
            api_url=os.getenv("SCHEDULER_API_URL", cls.api_url),
            timezone=os.getenv("SCHEDULER_TIMEZONE", cls.timezone),
            host=os.getenv("SCHEDULER_HOST", cls.host),
            port=int(os.getenv("SCHEDULER_PORT", cls.port)),
            max_workers=int(os.getenv("SCHEDULER_MAX_WORKERS", cls.max_workers)),
            default_timeout=int(os.getenv("SCHEDULER_DEFAULT_TIMEOUT", cls.default_timeout)),
        )
