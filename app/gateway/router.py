"""网关路由 — 所有渠道的入口(Phase 2:WebChat + CLI)。

接口:
  POST /api/sessions                          新建会话
  GET  /api/sessions                          会话列表
  GET  /api/sessions/{id}/messages            历史消息(展示格式)
  POST /api/sessions/{id}/chat/stream         发消息,SSE 流式返回

Phase 2 落库:会话/消息存 PostgreSQL,重启不丢。
每次 Agent 运行的模型、token 用量、耗时记进消息的 meta 字段。

流式桥接:agent_loop 是同步代码,跑在线程池里;
事件通过 asyncio.Queue 传回事件循环,再包成 SSE 帧推给前端。
Queue.put_nowait 是线程安全的,所以 sink 回调不需要任何锁。
"""
import asyncio
import json
import logging
import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.loop import SYSTEM, agent_loop
from app.config import settings
from app.core.sessions import Session, store
from app.db import get_db

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
async def create_session(db: AsyncSession = Depends(get_db),
                         _: str = Depends(require_gateway_token)):
    s = await store.create(db)
    return {"id": s.id, "title": s.title, "created_at": s.created_at}


@router.get("/sessions")
async def list_sessions(db: AsyncSession = Depends(get_db),
                        _: str = Depends(require_gateway_token)):
    return [{"id": s.id, "title": s.title, "created_at": s.created_at}
            for s in await store.list(db)]


@router.get("/sessions/{session_id}/messages")
async def list_messages(session_id: str, db: AsyncSession = Depends(get_db),
                        _: str = Depends(require_gateway_token)):
    s = await store.get(db, session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    return s.display


@router.post("/sessions/{session_id}/chat/stream")
async def chat_stream(session_id: str, body: ChatIn,
                      db: AsyncSession = Depends(get_db),
                      _: str = Depends(require_gateway_token)):
    s = await store.get(db, session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    if s.lock.locked():
        raise HTTPException(status_code=409, detail="该会话正在处理上一条消息,请稍候")

    async with s.lock:
        user_display = {"role": "user", "content": body.content}
        await store.append_display(db, s, user_display)
        _auto_title(s, body.content)
        await store.save_title(db, s)
        s.llm_messages.append({"role": "user", "content": body.content})
        # 用户消息先落库:就算 Agent 中途出错,数据库也停在"用户消息已入账"的
        # 干净状态,不会留下半截工具调用。出错时无需回滚——下次请求重新从库读。
        await store.save_llm(db, s)

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

            loop = asyncio.get_running_loop()
            started = time.perf_counter()
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
                return  # llm_messages 已在运行前落库,库里就是干净状态

            if cur:
                steps.append({"type": "text", "content": cur})
            assistant_display = {"role": "assistant", "steps": steps}
            # 每次调用留档:模型、token 用量、耗时(对齐项目 1 Playground 的要求)
            meta = {
                "model": settings.model_id,
                "usage": usage,
                "duration_ms": round((time.perf_counter() - started) * 1000),
            }
            await store.append_display(db, s, assistant_display, meta)
            await store.save_llm(db, s)
            yield _sse({"type": "done", "usage": usage,
                        "message": assistant_display, "meta": meta})

        # ponytail: 客户端中途断连时,Agent 线程会继续把任务跑完(结果照常进会话),
        # 只是没人接收事件。个人项目可接受,Phase 3 上任务队列后统一治理。
        return StreamingResponse(event_gen(), media_type="text/event-stream")
