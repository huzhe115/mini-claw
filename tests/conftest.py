import os
import tempfile

# 必须在导入 app 之前设置:测试用临时 workspace + 测试 token,
# 空 API key 保证任何漏网的代码都不会真调外部 LLM
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["GATEWAY_TOKEN"] = "test-token"
os.environ["WORKSPACE_DIR"] = tempfile.mkdtemp(prefix="openclaw-test-ws-")

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.sessions import store
from app.main import app


@pytest_asyncio.fixture(autouse=True)
async def clean_store():
    """每个测试前清空会话存储,测试之间互不污染。"""
    store._sessions.clear()
    yield


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def auth_headers():
    return {"X-Gateway-Token": "test-token"}
