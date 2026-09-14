import os
import tempfile

# 必须在导入 app 之前设置:测试用临时 workspace + 测试 token + 独立测试库,
# 空 API key 保证任何漏网的代码都不会真调外部 LLM
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["GATEWAY_TOKEN"] = "test-token"
os.environ["WORKSPACE_DIR"] = tempfile.mkdtemp(prefix="mini-claw-test-ws-")
os.environ["DATABASE_URL"] = "postgresql+asyncpg://postgres:123456@localhost:5432/mini_claw_test"

import asyncpg
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.db import Base, async_session, engine
from app.main import app

TEST_DB = "mini_claw_test"


@pytest_asyncio.fixture(scope="session", autouse=True)
async def ensure_test_db():
    """测试库不存在就建一个(连的是管理库 postgres)。"""
    conn = await asyncpg.connect(host="localhost", port=5432, user="postgres",
                                 password="123456", database="postgres")
    exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", TEST_DB)
    if not exists:
        await conn.execute(f'CREATE DATABASE "{TEST_DB}"')
    await conn.close()


@pytest_asyncio.fixture(autouse=True)
async def clean_db():
    """每个测试前重建所有表,测试之间互不污染。"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def auth_headers():
    return {"X-Gateway-Token": "test-token"}


@pytest_asyncio.fixture
async def db_session():
    async with async_session() as session:
        yield session
