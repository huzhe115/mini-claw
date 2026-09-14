"""Redis 客户端 — 前台小黑板。

Phase 4 只用它做限流计数。原则:Redis 挂了自动降级(限流失效但服务可用),
个人助手可用性优先,和 ai-chat-backend 的降级模式一致。
"""
import logging

import redis.asyncio as redis

from app.config import settings

logger = logging.getLogger(__name__)


class RedisClient:
    def __init__(self, url: str):
        self.url = url
        self._client: redis.Redis | None = None

    @property
    def client(self) -> redis.Redis:
        if self._client is None:
            self._client = redis.from_url(self.url, decode_responses=True)
        return self._client

    async def incr_window(self, key: str, ttl: int) -> int | None:
        """INCR + 设过期,返回当前窗口计数。Redis 不可用时返回 None(调用方降级)。"""
        try:
            pipe = self.client.pipeline()
            pipe.incr(key)
            pipe.expire(key, ttl)
            results = await pipe.execute()
            return int(results[0])
        except Exception as e:
            logger.warning("Redis 不可用,降级放行: %s", e)
            return None


redis_client = RedisClient(settings.redis_url)
