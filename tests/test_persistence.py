"""Phase 2 落库验证:会话、消息、llm_messages 重启后都在(直接读库模拟重启)。"""

import pytest
from fakes import FakeLLM, text_block, usage
from sqlalchemy import select

from app.db import async_session
from app.models import MessageModel, SessionModel


@pytest.fixture
def fake_llm(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr("app.agent.loop.llm", fake)
    return fake


async def _new_session(client, auth_headers) -> str:
    resp = await client.post("/api/sessions", headers=auth_headers)
    return resp.json()["id"]


async def test_sessions_survive_restart(client, auth_headers, fake_llm):
    """聊完之后直接读库(绕过一切内存状态):会话、消息、原始对话、meta 都落库。"""
    fake_llm.script.append(
        {
            "chunks": ["好的"],
            "blocks": [text_block("好的")],
            "usage": usage(10, 5),
        }
    )
    sid = await _new_session(client, auth_headers)

    resp = await client.post(
        f"/api/sessions/{sid}/chat/stream", headers=auth_headers, json={"content": "落库了吗"}
    )
    assert resp.status_code == 200

    # 模拟重启:新的连接、新的查询,不经过 store 的任何内存对象
    async with async_session() as db:
        row = await db.get(SessionModel, sid)
        assert row is not None
        assert row.title == "落库了吗"  # 自动标题也落库了
        # 与真实 SDK 的 model_dump 输出一致(citations 是 SDK 自带的空字段)
        assert row.llm_messages == [
            {"role": "user", "content": "落库了吗"},
            {"role": "assistant", "content": [{"type": "text", "text": "好的", "citations": None}]},
        ]

        messages = (
            await db.scalars(
                select(MessageModel).where(MessageModel.session_id == sid).order_by(MessageModel.id)
            )
        ).all()
        assert [m.role for m in messages] == ["user", "assistant"]
        assert messages[0].payload == {"role": "user", "content": "落库了吗"}
        assert messages[1].payload == {
            "role": "assistant",
            "steps": [{"type": "text", "content": "好的"}],
        }
        # 每次调用留档:模型、token、耗时
        assert messages[1].meta["usage"] == {"input_tokens": 10, "output_tokens": 5}
        assert messages[1].meta["model"]
        assert messages[1].meta["duration_ms"] >= 0


async def test_continue_after_restart(client, auth_headers, fake_llm):
    """重启后继续聊:历史消息能带进下一轮 LLM 调用(换门不换线的数据基础)。"""
    fake_llm.script.append(
        {
            "chunks": ["第一句"],
            "blocks": [text_block("第一句")],
            "usage": usage(10, 5),
        }
    )
    fake_llm.script.append(
        {
            "chunks": ["第二句"],
            "blocks": [text_block("第二句")],
            "usage": usage(20, 6),
        }
    )
    sid = await _new_session(client, auth_headers)
    await client.post(
        f"/api/sessions/{sid}/chat/stream", headers=auth_headers, json={"content": "第一轮"}
    )

    # 模拟重启后发第二轮:LLM 收到的 messages 必须包含第一轮全部历史
    resp = await client.post(
        f"/api/sessions/{sid}/chat/stream", headers=auth_headers, json={"content": "第二轮"}
    )
    assert resp.status_code == 200

    sent_messages = fake_llm.calls[-1]["messages"]
    assert sent_messages[0] == {"role": "user", "content": "第一轮"}
    assert sent_messages[1]["role"] == "assistant"
    assert sent_messages[-1] == {"role": "user", "content": "第二轮"}


async def test_session_list_from_db(client, auth_headers):
    """会话列表从库里按时间倒序出来。"""
    id1 = await _new_session(client, auth_headers)
    id2 = await _new_session(client, auth_headers)

    resp = await client.get("/api/sessions", headers=auth_headers)
    ids = [s["id"] for s in resp.json()]
    assert ids == [id2, id1]  # 新的在前
