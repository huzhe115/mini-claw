"""排班表(cron):CRUD、表达式校验、报时员敲门、忙时跳过。"""
import pytest
from fakes import FakeLLM, text_block, usage

from app.core.sessions import store
from app.models import CronJobModel


@pytest.fixture
def fake_llm(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr("app.agent.loop.llm", fake)
    return fake


async def _new_session(client, auth_headers) -> str:
    resp = await client.post("/api/sessions", headers=auth_headers)
    return resp.json()["id"]


async def test_cron_crud_and_bad_schedule(client, auth_headers):
    sid = await _new_session(client, auth_headers)

    # 非法 cron 表达式 → 400
    resp = await client.post("/api/cron", headers=auth_headers,
                             json={"session_id": sid, "prompt": "p",
                                   "schedule": "not a cron"})
    assert resp.status_code == 400

    # 正常创建
    resp = await client.post("/api/cron", headers=auth_headers,
                             json={"session_id": sid, "prompt": "每天早上 8 点发天气",
                                   "schedule": "0 8 * * *"})
    assert resp.status_code == 201
    job = resp.json()
    assert job["prompt"] == "每天早上 8 点发天气"
    assert job["enabled"] is True

    # 目标会话不存在 → 404
    resp = await client.post("/api/cron", headers=auth_headers,
                             json={"session_id": "nope", "prompt": "p",
                                   "schedule": "0 8 * * *"})
    assert resp.status_code == 404

    # 列表 + 删除
    resp = await client.get("/api/cron", headers=auth_headers)
    assert [j["id"] for j in resp.json()] == [job["id"]]
    resp = await client.delete(f"/api/cron/{job['id']}", headers=auth_headers)
    assert resp.status_code == 204
    resp = await client.get("/api/cron", headers=auth_headers)
    assert resp.json() == []


async def test_fire_cron_runs_agent(client, auth_headers, fake_llm, db_session):
    """报时员敲门:以 cron 角色注入消息,Agent 跑一轮,结果留在会话里。"""
    fake_llm.script.append({
        "chunks": ["早上好,今天多云"],
        "blocks": [text_block("早上好,今天多云")],
        "usage": usage(5, 3),
    })
    sid = await _new_session(client, auth_headers)
    resp = await client.post("/api/cron", headers=auth_headers,
                             json={"session_id": sid, "prompt": "给用户发早安",
                                   "schedule": "0 8 * * *"})
    job_id = resp.json()["id"]

    resp = await client.post(f"/api/cron/{job_id}/run", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["steps"][0]["content"] == "早上好,今天多云"

    # 会话里出现 cron 消息 + 助手回复;last_run_at 已更新
    session = await store.get(db_session, sid)
    assert [m["role"] for m in session.display] == ["cron", "assistant"]
    job = await db_session.get(CronJobModel, job_id)
    assert job.last_run_at is not None


async def test_fire_cron_skips_busy_session(client, auth_headers, db_session):
    """用户正在聊时,报时员不插话。"""
    sid = await _new_session(client, auth_headers)
    resp = await client.post("/api/cron", headers=auth_headers,
                             json={"session_id": sid, "prompt": "发早安",
                                   "schedule": "0 8 * * *"})
    job_id = resp.json()["id"]

    session = await store.get(db_session, sid)
    await session.lock.acquire()
    resp = await client.post(f"/api/cron/{job_id}/run", headers=auth_headers)
    assert resp.json() == {"skipped": "session busy"}
    session.lock.release()


async def test_fire_cron_missing_job(client, auth_headers):
    resp = await client.post("/api/cron/999/run", headers=auth_headers)
    assert resp.json() == {"skipped": "job missing or disabled"}
