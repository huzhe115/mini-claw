"""网关路由 — 所有渠道的入口(Phase 1 只有 WebChat)。

接口:
  POST /api/sessions                          新建会话
  GET  /api/sessions                          会话列表
  GET  /api/sessions/{id}/messages            历史消息(展示格式)
  POST /api/sessions/{id}/chat/stream         发消息,SSE 流式返回

流式桥接(本文件的重点):agent_loop 是同步代码,跑在线程池里;
事件通过 asyncio.Queue 传回事件循环,再包成 SSE 帧推给前端。
Queue.put_nowait 是线程安全的,所以 sink 回调不需要任何锁。
"""
import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent.loop import SYSTEM, agent_loop
from app.core.sessions import Session, store

from .auth import require_gateway_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["gateway"])


def _sse(data: dict) -> str:
    """把一个事件包成 SSE 帧(空行是事件边界)。"""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


class ChatIn(BaseModel):
    content: str = Field(min_length=1, max_length=10000)


def _auto_title(session: Session, content: str) -> None:
    """首条消息把默认标题换成话题标题(截断前 16 字,像 DeepSeek 侧栏)。"""
    if session.title == "新会话":
        flat = " ".join(content.split())
        session.title = flat[:16] + ("…" if len(flat) > 16 else "")


def _run_agent_sync(messages: list, sink) -> Exception | None:
    """跑在线程池里的同步 Agent。正常返回 None;异常先发 error 事件再返回异常对象。

    结束哨兵 None 必须由本函数发出(无论成败)——消费端靠它知道"流结束了"。
    """
    try:
        agent_loop(messages, system=SYSTEM, sink=sink)
        return None
    except Exception as e:
        sink({"type": "error", "detail": str(e)})
        return e
    finally:
        sink(None)


@router.post("/sessions", status_code=201)
async def create_session(_: str = Depends(require_gateway_token)):
    s = await store.create()
    return {"id": s.id, "title": s.title, "created_at": s.created_at}


@router.get("/sessions")
async def list_sessions(_: str = Depends(require_gateway_token)):
    return [{"id": s.id, "title": s.title, "created_at": s.created_at}
            for s in await store.list()]


@router.get("/sessions/{session_id}/messages")
async def list_messages(session_id: str, _: str = Depends(require_gateway_token)):
    s = await store.get(session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    return s.display


@router.post("/sessions/{session_id}/chat/stream")
async def chat_stream(session_id: str, body: ChatIn,
                      _: str = Depends(require_gateway_token)):
    s = await store.get(session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    if s.lock.locked():
        raise HTTPException(status_code=409, detail="该会话正在处理上一条消息,请稍候")

    async with s.lock:
        user_display = {"role": "user", "content": body.content}
        s.display.append(user_display)
        _auto_title(s, body.content)
        s.llm_messages.append({"role": "user", "content": body.content})

        queue: asyncio.Queue = asyncio.Queue()

        def sink(ev) -> None:
            # 在 Agent 线程里被调用;put_nowait 线程安全,结束哨兵是 None
            queue.put_nowait(ev)

        async def event_gen():
            # 展示步骤在这里累积(只有本协程在消费队列,单线程无锁):
            # delta 攒成 text 步,工具事件单独成步,结果挂回对应工具步
            steps: list[dict] = []
            cur = ""
            usage = None
            yield _sse({"type": "user_msg", "message": user_display})

            snapshot = s.llm_messages[:]  # 出错时回滚,不留半截工具调用
            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(None, _run_agent_sync, s.llm_messages, sink)

            while True:
                ev = await queue.get()
                if ev is None:  # Agent 结束哨兵
                    break
                kind = ev["type"]
                if kind == "delta":
                    cur += ev["text"]
                    yield _sse(ev)
                elif kind == "tool":
                    if cur:
                        steps.append({"type": "text", "content": cur})
                        cur = ""
                    steps.append({"type": "tool", "name": ev["name"],
                                  "input": ev["input"], "output": ""})
                    yield _sse(ev)
                elif kind == "tool_result":
                    # 倒序找最近一个还没结果的工具步挂上去
                    for st in reversed(steps):
                        if st["type"] == "tool" and not st.get("output"):
                            st["output"] = ev["output"]
                            break
                    yield _sse(ev)
                elif kind == "done":
                    usage = ev.get("usage")
                elif kind == "error":
                    yield _sse(ev)

            err = await future
            if err is not None:
                s.llm_messages[:] = snapshot  # 回滚到本次运行前
                return

            if cur:
                steps.append({"type": "text", "content": cur})
            assistant_display = {"role": "assistant", "steps": steps}
            s.display.append(assistant_display)
            yield _sse({"type": "done", "usage": usage, "message": assistant_display})

        # ponytail: 客户端中途断连时,Agent 线程会继续把任务跑完(结果照常进会话),
        # 只是没人接收事件。个人项目可接受,Phase 3 上任务队列后统一治理。
        return StreamingResponse(event_gen(), media_type="text/event-stream")
