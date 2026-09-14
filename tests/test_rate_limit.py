"""限流:计数逻辑(假 Redis)与网关 429。"""
import pytest

from app.rate_limit import check_rate_limit


@pytest.fixture(autouse=True)
def enable_limit(monkeypatch):
    """conftest 全局关了限流(RATE_LIMIT_PER_MINUTE=0),这里单独开回来。"""
    monkeypatch.setattr("app.rate_limit.settings.rate_limit_per_minute", 30)


class FakeRedis:
    """假 Redis:记录 incr_window 调用,按脚本返回计数。"""

    def __init__(self):
        self.calls = []
        self.counts = []  # 每次返回的计数,pop 使用

    async def incr_window(self, key, ttl):
        self.calls.append((key, ttl))
        if self.counts:
            return self.counts.pop(0)
        return 1


async def test_check_rate_limit_counts(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr("app.rate_limit.redis_client", fake)
    assert await check_rate_limit("tok") is True  # 1 <= 30
    assert fake.calls[0][1] == 60  # 窗口 60 秒
    assert fake.calls[0][0].startswith("rate:")  # key 用 token 哈希


async def test_check_rate_limit_blocks(monkeypatch):
    fake = FakeRedis()
    fake.counts = [31]
    monkeypatch.setattr("app.rate_limit.redis_client", fake)
    assert await check_rate_limit("tok") is False


async def test_check_rate_limit_fail_open(monkeypatch):
    """Redis 挂了降级放行:真实客户端不可用时返回 None,限流层据此放行。"""

    class BrokenRedis:
        async def incr_window(self, key, ttl):
            return None  # 真实 RedisClient.incr_window 的降级行为

    monkeypatch.setattr("app.rate_limit.redis_client", BrokenRedis())
    assert await check_rate_limit("tok") is True


async def test_chat_stream_429(client, auth_headers, monkeypatch):
    """超限时网关直接 429,不进 Agent。"""
    async def blocked(token):
        return False
    monkeypatch.setattr("app.gateway.router.check_rate_limit", blocked)

    resp = await client.post("/api/sessions", headers=auth_headers)
    sid = resp.json()["id"]
    resp = await client.post(f"/api/sessions/{sid}/chat/stream",
                             headers=auth_headers, json={"content": "hi"})
    assert resp.status_code == 429
