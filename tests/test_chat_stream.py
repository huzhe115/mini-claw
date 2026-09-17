"""流式聊天接口:用假 LLM 验证 网页 → 网关 → Agent → SSE 的完整链路。"""

import asyncio
import json

import pytest
from fakes import FakeLLM, text_block, tool_use_block, usage

from app.agent.loop import agent_loop
from app.core.sessions import store


def parse_sse(text: str) -> list[dict]:
    """把 SSE 响应体解析成事件列表。"""
    events = []
    for frame in text.split("\n\n"):
        for line in frame.split("\n"):
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


@pytest.fixture
def fake_llm(monkeypatch):
    fake = FakeLLM()
    # loop.py 里是 `from .llm import llm`,补的是 loop 模块里的名字
    monkeypatch.setattr("app.agent.loop.llm", fake)
    return fake


async def _new_session(client, auth_headers) -> str:
    resp = await client.post("/api/sessions", headers=auth_headers)
    return resp.json()["id"]


async def test_plain_reply(client, auth_headers, fake_llm):
    """模型不调工具:delta 逐段流式推送,收尾 done 带完整消息。"""
    fake_llm.script.append(
        {
            "chunks": ["你好", ",世界"],
            "blocks": [text_block("你好,世界")],
            "usage": usage(10, 5),
        }
    )
    sid = await _new_session(client, auth_headers)

    resp = await client.post(
        f"/api/sessions/{sid}/chat/stream", headers=auth_headers, json={"content": "打个招呼"}
    )
    assert resp.status_code == 200
    events = parse_sse(resp.text)

    assert events[0] == {"type": "user_msg", "message": {"role": "user", "content": "打个招呼"}}
    assert [e["text"] for e in events if e["type"] == "delta"] == ["你好", ",世界"]
    done = events[-1]
    assert done["type"] == "done"
    assert done["usage"] == {"input_tokens": 10, "output_tokens": 5}
    assert done["message"] == {
        "role": "assistant",
        "steps": [{"type": "text", "content": "你好,世界"}],
    }

    # 历史接口能看到完整对话
    resp = await client.get(f"/api/sessions/{sid}/messages", headers=auth_headers)
    assert len(resp.json()) == 2  # user + assistant


async def test_tool_call_flow(client, auth_headers, fake_llm, db_session):
    """模型调 bash 工具:tool/tool_result 事件、展示步骤、消息回填都要对。"""
    fake_llm.script.append(
        {
            "chunks": [],
            "blocks": [tool_use_block("bash", {"command": "echo hello"})],
        }
    )
    fake_llm.script.append(
        {
            "chunks": [],
            "blocks": [text_block("执行完毕")],
            "usage": usage(20, 8),
        }
    )
    sid = await _new_session(client, auth_headers)

    resp = await client.post(
        f"/api/sessions/{sid}/chat/stream", headers=auth_headers, json={"content": "跑一下命令"}
    )
    events = parse_sse(resp.text)

    tool_ev = next(e for e in events if e["type"] == "tool")
    assert tool_ev["name"] == "bash"
    result_ev = next(e for e in events if e["type"] == "tool_result")
    assert "hello" in result_ev["output"]

    # 展示消息:文本步 + 工具步(结果已挂回)
    done = events[-1]
    steps = done["message"]["steps"]
    assert steps[0] == {
        "type": "tool",
        "name": "bash",
        "input": {"command": "echo hello"},
        "output": result_ev["output"],
    }
    assert steps[1] == {"type": "text", "content": "执行完毕"}

    # 喂给模型的原始消息里,工具结果用 tool_use_id 回填
    session = await store.get(db_session, sid)
    tool_result_msg = session.llm_messages[-2]  # 倒二条是 tool_result 回填
    assert tool_result_msg["role"] == "user"
    assert tool_result_msg["content"][0]["tool_use_id"] == "toolu_1"


async def test_error_rollback(client, auth_headers, fake_llm, db_session):
    """LLM 中途报错:前端收到 error 事件,会话消息回滚到发送前。"""
    fake_llm.script.append(
        {
            "chunks": [],
            "blocks": [],
            "error": RuntimeError("boom"),
        }
    )
    sid = await _new_session(client, auth_headers)

    resp = await client.post(
        f"/api/sessions/{sid}/chat/stream", headers=auth_headers, json={"content": "会出错吗"}
    )
    events = parse_sse(resp.text)
    assert events[-1]["type"] == "error"
    assert "boom" in events[-1]["detail"]

    session = await store.get(db_session, sid)
    # 用户消息在运行前已落库,Agent 报错后库里就是干净状态(没有半截工具调用)
    assert session.llm_messages == [{"role": "user", "content": "会出错吗"}]
    assert len(session.display) == 1  # 只有用户消息,没有半截助手消息


async def test_busy_session_queues_then_continues(client, auth_headers, fake_llm, db_session):
    """cron 占着会话锁时,聊天请求排队等锁;先发 queued 帧,锁一释放照常处理。"""
    fake_llm.script.append(
        {
            "chunks": ["排队成功"],
            "blocks": [text_block("排队成功")],
            "usage": usage(5, 3),
        }
    )
    sid = await _new_session(client, auth_headers)
    session = await store.get(db_session, sid)
    await session.lock.acquire()

    task = asyncio.create_task(
        client.post(
            f"/api/sessions/{sid}/chat/stream", headers=auth_headers, json={"content": "插队"}
        )
    )

    # 等请求走到等锁这一步(排进锁的等待队列)再释放,不靠固定 sleep 赌时序
    for _ in range(200):
        if session.lock._waiters:
            break
        await asyncio.sleep(0.01)
    assert session.lock._waiters, "请求没有排队等锁"
    session.lock.release()

    resp = await task
    assert resp.status_code == 200
    events = parse_sse(resp.text)
    assert events[0] == {"type": "queued", "task": "插队", "wait": 330}
    assert events[1]["message"]["content"] == "插队"
    assert [e["text"] for e in events if e["type"] == "delta"] == ["排队成功"]


async def test_wait_task_does_not_block_chat(client, auth_headers, fake_llm, db_session):
    """用户复现场景:agent 在跑 wait 倒计时(锁被占)时,新消息排队等锁,
    倒计时结束后照常处理,不再"发不了消息"。"""
    fake_llm.script.append(
        {
            "chunks": [],
            "blocks": [tool_use_block("wait", {"seconds": 2})],
        }
    )
    fake_llm.script.append(
        {
            "chunks": ["倒计时结束"],
            "blocks": [text_block("倒计时结束")],
            "usage": usage(5, 3),
        }
    )
    fake_llm.script.append(
        {
            "chunks": ["第二条也通了"],
            "blocks": [text_block("第二条也通了")],
            "usage": usage(3, 2),
        }
    )
    sid = await _new_session(client, auth_headers)
    session = await store.get(db_session, sid)

    task1 = asyncio.create_task(
        client.post(
            f"/api/sessions/{sid}/chat/stream",
            headers=auth_headers,
            json={"content": "2 秒后提醒我"},
        )
    )
    # 等第一条跑到 wait、锁被占,再发第二条
    for _ in range(200):
        if session.lock.locked():
            break
        await asyncio.sleep(0.01)
    assert session.lock.locked(), "第一条消息没有进入倒计时"
    task2 = asyncio.create_task(
        client.post(
            f"/api/sessions/{sid}/chat/stream", headers=auth_headers, json={"content": "排队消息"}
        )
    )

    events1 = parse_sse((await task1).text)
    assert "倒计时结束" in [e["text"] for e in events1 if e["type"] == "delta"]

    events2 = parse_sse((await task2).text)
    assert events2[0]["type"] == "queued"  # 先收到排队帧
    assert "第二条也通了" in [e["text"] for e in events2 if e["type"] == "delta"]


async def test_busy_session_wait_timeout(client, auth_headers, db_session, monkeypatch):
    """锁被占且排队超时(锁持有太久):收到 error 帧,流正常结束。"""
    monkeypatch.setattr("app.gateway.router.settings.chat_lock_wait", 0.05)
    sid = await _new_session(client, auth_headers)
    session = await store.get(db_session, sid)
    await session.lock.acquire()

    resp = await client.post(
        f"/api/sessions/{sid}/chat/stream", headers=auth_headers, json={"content": "插队"}
    )
    assert resp.status_code == 200
    events = parse_sse(resp.text)
    assert events[0]["type"] == "queued"
    assert events[-1]["type"] == "error"
    assert "任务还没跑完" in events[-1]["detail"]
    session.lock.release()


async def test_agent_loop_direct(fake_llm):
    """直接调 agent_loop(不走 HTTP):验证循环本身的事件序列。"""
    fake_llm.script.append(
        {
            "chunks": ["答"],
            "blocks": [text_block("答案")],
            "usage": usage(3, 1),
        }
    )
    events = []
    messages = [{"role": "user", "content": "问"}]
    agent_loop(messages, sink=events.append)

    assert events[-1]["type"] == "done"
    assert events[-1]["usage"] == {"input_tokens": 3, "output_tokens": 1}
    assert messages[-1]["role"] == "assistant"  # 回复已进消息列表
