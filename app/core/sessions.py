"""会话存储 — Phase 1 内存版。

会话 = 一条对话线(对应原版 OpenClaw 的 dmScope=per-channel-peer)。
每个 Session 存两份数据:
- llm_messages: 喂给模型的原始消息(Anthropic 格式,含 tool_use/tool_result 块)
- display:      给前端看的展示消息(步骤列表:文本 + 工具调用轨迹)
两者各存各的互不翻译,省得展示层和模型层耦合。

Phase 2 落 PostgreSQL,对外接口保持不变。
"""
import asyncio
import time
import uuid


class Session:
    def __init__(self):
        self.id = uuid.uuid4().hex[:12]
        self.created_at = time.time()
        self.title = "新会话"
        self.llm_messages: list[dict] = []
        self.display: list[dict] = []
        self.lock = asyncio.Lock()  # 同一会话同一时间只处理一条消息


class SessionStore:
    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._guard = asyncio.Lock()  # 保护字典本身的并发读写

    async def create(self) -> Session:
        s = Session()
        async with self._guard:
            self._sessions[s.id] = s
        return s

    async def get(self, session_id: str) -> Session | None:
        async with self._guard:
            return self._sessions.get(session_id)

    async def list(self) -> list[Session]:
        async with self._guard:
            return sorted(self._sessions.values(), key=lambda s: s.created_at, reverse=True)


store = SessionStore()
