"""会话存储 — Phase 2 PostgreSQL 版。

Phase 1 是内存 dict,重启即失;Phase 2 落库:
- sessions 表:会话本体 + llm_messages(喂给模型的原始消息,JSONB)
- messages 表:展示消息(user 纯文本 / assistant 步骤列表,JSONB)
两份数据各存各的互不翻译——Phase 1 的原则延续。

对外接口和 Phase 1 相同:store.create/get/list + Session 对象。
一个注意点:get 每次从库里新造 Session 对象,所以会话锁必须放在
进程级注册表里按 id 共享,否则两个并发请求各拿各的锁,锁就失效了。
"""

import asyncio
import contextvars
import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MessageModel, SessionModel


def _dump(obj):
    """把 anthropic SDK 的内容块(pydantic 对象)转成可 JSON 化的 dict。"""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return str(obj)


def _dump_llm_messages(messages: list) -> list:
    """llm_messages 里混着 SDK 块对象和普通 dict,先整体归一成纯 dict 再入库。"""
    return json.loads(json.dumps(messages, default=_dump, ensure_ascii=False))


# 会话锁注册表:id → asyncio.Lock。单进程网关够用,多进程部署时换分布式锁
_locks: dict[str, asyncio.Lock] = {}

# ---------- 跨线程会话上下文(工具在 Agent 线程里跑,靠它们找到回家的路) ----------
# 当前回合的会话 id,由 run_turn 设置。工具层(如 wait 注册提醒任务)靠它知道
# "这条提醒属于哪个会话"。Agent 线程通过 copy_context 继承同一份上下文。
session_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("session_id", default="")

# 主事件循环引用,由 run_turn 设置。工具的异步写库(如 create_cron 落库)
# 要通过 run_coroutine_threadsafe 提交回主循环执行。
main_loop: asyncio.AbstractEventLoop | None = None


def session_lock(session_id: str) -> asyncio.Lock:
    lock = _locks.get(session_id)
    if lock is None:
        lock = _locks[session_id] = asyncio.Lock()
    return lock


class Session:
    def __init__(self, id: str, title: str, created_at, llm_messages: list, display: list):
        self.id = id
        self.title = title
        self.created_at = created_at
        self.llm_messages = llm_messages
        self.display = display

    @property
    def lock(self) -> asyncio.Lock:
        return session_lock(self.id)


class SessionStore:
    async def create(self, db: AsyncSession) -> Session:
        row = SessionModel(id=uuid.uuid4().hex[:12])
        db.add(row)
        await db.commit()
        await db.refresh(row)  # 拿数据库生成的 created_at
        return Session(row.id, row.title, row.created_at, [], [])

    async def get(self, db: AsyncSession, session_id: str) -> Session | None:
        row = await db.get(SessionModel, session_id)
        if row is None:
            return None
        messages = (
            await db.scalars(
                select(MessageModel)
                .where(MessageModel.session_id == session_id)
                .order_by(MessageModel.id)
            )
        ).all()
        return Session(
            row.id, row.title, row.created_at, row.llm_messages or [], [m.payload for m in messages]
        )

    async def list(self, db: AsyncSession) -> list[Session]:
        rows = (
            await db.scalars(select(SessionModel).order_by(SessionModel.created_at.desc()))
        ).all()
        # 列表页只要头部信息,不加载消息
        return [Session(r.id, r.title, r.created_at, [], []) for r in rows]

    async def save_title(self, db: AsyncSession, s: Session) -> None:
        row = await db.get(SessionModel, s.id)
        row.title = s.title
        await db.commit()

    async def append_display(
        self, db: AsyncSession, s: Session, entry: dict, meta: dict | None = None
    ) -> None:
        db.add(MessageModel(session_id=s.id, role=entry["role"], payload=entry, meta=meta))
        await db.commit()
        s.display.append(entry)

    async def save_llm(self, db: AsyncSession, s: Session) -> None:
        row = await db.get(SessionModel, s.id)
        row.llm_messages = _dump_llm_messages(s.llm_messages)
        await db.commit()


store = SessionStore()
