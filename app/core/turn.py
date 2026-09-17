"""一轮 Agent 的完整流程 — chat 和 cron 两个调用方共用。

调用方负责:取会话、判断并持有会话锁(chat 排队等锁、超时报 409,cron 拿不到锁跳过)。
本函数在已加锁的会话里做:
  用户消息落库(role 区分来源:user / cron)→ 跑 Agent → 助手消息落库(带 meta)
  → 后台提取跨会话记忆。
返回 {"steps", "usage", "meta"} 或 {"error": 原因}。
"""

import asyncio
import contextvars
import time

from app.agent.loop import SYSTEM, agent_loop
from app.agent.memory import MEMORY, memory_section
from app.config import settings
from app.core import sessions as sessions_mod
from app.core.sessions import Session, session_ctx, store


def _auto_title(session: Session, content: str) -> None:
    """首条消息把默认标题换成话题标题(截断前 16 字,像 DeepSeek 侧栏)。"""
    if session.title == "新会话":
        flat = " ".join(content.split())
        session.title = flat[:16] + ("…" if len(flat) > 16 else "")


def _run_agent_sync(messages: list, content: str, sink) -> Exception | None:
    """跑在线程池里的同步 Agent。正常返回 None;异常先发 error 事件再返回异常对象。

    进入主循环前先召回相关记忆拼进 system prompt(记事本查一遍再干活)。
    结束哨兵 None 必须由本函数发出(无论成败)——消费端靠它知道"流结束了"。
    """
    from datetime import datetime

    try:
        # 报时:模型不知道"现在几点",没法把"5点16叫我"转成准确的 24h 时刻
        system = SYSTEM + (
            f"\nCurrent local time: {datetime.now():%Y-%m-%d %H:%M %A}. "
            "When the user names a clock time, convert it to 24h HH:MM for the wait tool."
        )
        if settings.memory_enabled:
            recalled = MEMORY.load_relevant(content)
            system = system + memory_section(recalled)
        agent_loop(messages, system=system, sink=sink)
        return None
    except Exception as e:
        sink({"type": "error", "detail": str(e)})
        return e
    finally:
        sink(None)


async def run_turn(db, session: Session, content: str, role: str = "user", sink=None) -> dict:
    """在已加锁的会话里跑一轮 Agent。

    sink: 收到 Agent 的全部事件,包括结束哨兵 None——外层消费者靠它知道流结束了。
    传 None 表示不外发。
    """
    emit = sink or (lambda ev: None)
    # 工具层(如 wait 注册提醒)需要知道当前会话;Agent 线程靠 ctx.run 继承同一份。
    # main_loop 让工具线程能把异步写库提交回主循环(见 tools.run_create_cron)。
    sessions_mod.main_loop = asyncio.get_running_loop()
    ctx_token = session_ctx.set(session.id)
    try:
        return await _run_turn_inner(db, session, content, role, emit)
    finally:
        session_ctx.reset(ctx_token)


async def _run_turn_inner(db, session: Session, content: str, role: str, emit) -> dict:
    display = {"role": role, "content": content}
    await store.append_display(db, session, display)
    if role == "user":
        _auto_title(session, content)
        await store.save_title(db, session)
    session.llm_messages.append({"role": "user", "content": content})
    # 用户消息先落库:就算 Agent 中途出错,数据库也停在"用户消息已入账"的
    # 干净状态,不会留下半截工具调用。出错时无需回滚——下次请求重新从库读。
    await store.save_llm(db, session)

    queue: asyncio.Queue = asyncio.Queue()

    def bridge(ev) -> None:
        # 在 Agent 线程里被调用;put_nowait 线程安全。
        # None 哨兵必须转发给外层——只进本函数自己的队列的话,外层会永远等不到结束
        queue.put_nowait(ev)
        emit(ev)

    started = time.perf_counter()
    loop = asyncio.get_running_loop()
    # ctx.run 把 session_ctx 带进 Agent 线程:工具层读得到当前会话 id
    ctx = contextvars.copy_context()
    future = loop.run_in_executor(
        None, ctx.run, _run_agent_sync, session.llm_messages, content, bridge
    )

    # 展示步骤在这里累积(只有本协程在消费队列,单线程无锁):
    # delta 攒成 text 步,工具事件单独成步,结果挂回对应工具步
    steps: list[dict] = []
    cur = ""
    usage = None
    err = None
    while True:
        ev = await queue.get()
        if ev is None:  # Agent 结束哨兵
            break
        kind = ev["type"]
        if kind == "delta":
            cur += ev["text"]
        elif kind == "tool":
            if cur:
                steps.append({"type": "text", "content": cur})
                cur = ""
            steps.append({"type": "tool", "name": ev["name"], "input": ev["input"], "output": ""})
        elif kind == "tool_result":
            # 倒序找最近一个还没结果的工具步挂上去
            for st in reversed(steps):
                if st["type"] == "tool" and not st.get("output"):
                    st["output"] = ev["output"]
                    break
        elif kind == "done":
            usage = ev.get("usage")
        elif kind == "error":
            err = ev["detail"]

    ex = await future
    if err is None and ex is not None:
        err = str(ex)
    if err is not None:
        return {"error": err}

    if cur:
        steps.append({"type": "text", "content": cur})
    assistant_display = {"role": "assistant", "steps": steps}
    # 每次调用留档:模型、token 用量、耗时(对齐项目 1 Playground 的要求)
    meta = {
        "model": settings.model_id,
        "usage": usage,
        "duration_ms": round((time.perf_counter() - started) * 1000),
    }
    await store.append_display(db, session, assistant_display, meta)
    await store.save_llm(db, session)

    # 回合结束后后台提取跨会话记忆:不阻塞流,提取失败只记日志
    if settings.memory_enabled:
        asyncio.get_running_loop().create_task(
            asyncio.to_thread(MEMORY.extract, list(session.llm_messages))
        )
    return {"steps": steps, "usage": usage, "meta": meta}
