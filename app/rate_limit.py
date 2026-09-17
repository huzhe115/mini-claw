"""网关限流 — 每个 token 每分钟 N 次(滑动窗口计数,Redis INCR + 60s 过期)。

Redis 挂了降级放行;rate_limit_per_minute <= 0 关闭限流。
"""

import hashlib

from app.config import settings
from app.redis_client import redis_client


async def check_rate_limit(token: str) -> bool:
    """True = 放行,False = 超限。"""
    limit = settings.rate_limit_per_minute
    if limit <= 0:
        return True
    key = "rate:" + hashlib.sha256(token.encode()).hexdigest()[:16]
    count = await redis_client.incr_window(key, 60)
    if count is None:
        return True  # Redis 不可用,降级放行
    return count <= limit
