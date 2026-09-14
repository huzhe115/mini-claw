"""流式聊天接口:用假 LLM 验证 网页 → 网关 → Agent → SSE 的完整链路。"""
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
    fake_llm.script.append({
        "chunks": ["你好", ",世界"],
        "blocks": [text_block("你好,世界")],
        "usage": usage(10, 5),
    })
    sid = await _new_session(client, auth_headers)

    resp = await client.post(f"/api/sessions/{sid}/chat/stream",
                             headers=auth_headers, json={"content": "打个招呼"})
    assert resp.status_code == 200
    events = parse_sse(resp.text)

    assert events[0] == {"type": "user_msg",
                         "message": {"role": "user", "content": "打个招呼"}}
    assert [e["text"] for e in events if e["type"] == "delta"] == ["你好", ",世界"]
    done = events[-1]
    assert done["type"] == "done"
    assert done["usage"] == {"input_tokens": 10, "output_tokens": 5}
    assert done["message"] == {"role": "assistant",
                               "steps": [{"type": "text", "content": "你好,世界"}]}

    # 历史接口能看到完整对话
    resp = await client.get(f"/api/sessions/{sid}/messages", headers=auth_headers)
    assert len(resp.json()) == 2  # user + assistant


async def test_tool_call_flow(client, auth_headers, fake_llm, db_session):
    """模型调 bash 工具:tool/tool_result 事件、展示步骤、消息回填都要对。"""
    fake_llm.script.append({
        "chunks": [],
        "blocks": [tool_use_block("bash", {"command": "echo hello"})],
    })
    fake_llm.script.append({
        "chunks": [],
        "blocks": [text_block("执行完毕")],
        "usage": usage(20, 8),
    })
    sid = await _new_session(client, auth_headers)

    resp = await client.post(f"/api/sessions/{sid}/chat/stream",
                             headers=auth_headers, json={"content": "跑一下命令"})
    events = parse_sse(resp.text)

    tool_ev = next(e for e in events if e["type"] == "tool")
    assert tool_ev["name"] == "bash"
    result_ev = next(e for e in events if e["type"] == "tool_result")
    assert "hello" in result_ev["output"]

    # 展示消息:文本步 + 工具步(结果已挂回)
    done = events[-1]
    steps = done["message"]["steps"]
    assert steps[0] == {"type": "tool", "name": "bash",
                        "input": {"command": "echo hello"}, "output": result_ev["output"]}
    assert steps[1] == {"type": "text", "content": "执行完毕"}

    # 喂给模型的原始消息里,工具结果用 tool_use_id 回填
    session = await store.get(db_session, sid)
    tool_result_msg = session.llm_messages[-2]  # 倒二条是 tool_result 回填
    assert tool_result_msg["role"] == "user"
    assert tool_result_msg["content"][0]["tool_use_id"] == "toolu_1"


async def test_error_rollback(client, auth_headers, fake_llm, db_session):
    """LLM 中途报错:前端收到 error 事件,会话消息回滚到发送前。"""
    fake_llm.script.append({
        "chunks": [],
        "blocks": [],
        "error": RuntimeError("boom"),
    })
    sid = await _new_session(client, auth_headers)

    resp = await client.post(f"/api/sessions/{sid}/chat/stream",
                             headers=auth_headers, json={"content": "会出错吗"})
    events = parse_sse(resp.text)
    assert events[-1]["type"] == "error"
    assert "boom" in events[-1]["detail"]

    session = await store.get(db_session, sid)
    # 用户消息在运行前已落库,Agent 报错后库里就是干净状态(没有半截工具调用)
    assert session.llm_messages == [{"role": "user", "content": "会出错吗"}]
    assert len(session.display) == 1  # 只有用户消息,没有半截助手消息


async def test_busy_session_409(client, auth_headers, db_session):
    """同一会话同时只处理一条消息:锁被占时返回 409。"""
    sid = await _new_session(client, auth_headers)
    session = await store.get(db_session, sid)
    await session.lock.acquire()

    resp = await client.post(f"/api/sessions/{sid}/chat/stream",
                             headers=auth_headers, json={"content": "插队"})
    assert resp.status_code == 409
    session.lock.release()


async def test_agent_loop_direct(fake_llm):
    """直接调 agent_loop(不走 HTTP):验证循环本身的事件序列。"""
    fake_llm.script.append({
        "chunks": ["答"],
        "blocks": [text_block("答案")],
        "usage": usage(3, 1),
    })
    events = []
    messages = [{"role": "user", "content": "问"}]
    agent_loop(messages, sink=events.append)

    assert events[-1]["type"] == "done"
    assert events[-1]["usage"] == {"input_tokens": 3, "output_tokens": 1}
    assert messages[-1]["role"] == "assistant"  # 回复已进消息列表
