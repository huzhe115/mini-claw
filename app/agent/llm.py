"""LLM 接入层 — Anthropic SDK 包一层。

Phase 1 只接 DeepSeek 的 Anthropic 兼容端点(和 mini-claude 相同,已验证可用)。
Phase 3 做供应商抽象:原版 OpenClaw 支持多模型路由/故障转移,
到时候这里改成按配置选择 provider,上层循环不用动。
"""

from anthropic import Anthropic

from app.config import settings


class LLM:
    def __init__(self):
        self._client: Anthropic | None = None

    @property
    def client(self) -> Anthropic:
        # 懒初始化:测试里换掉整个 llm 实例时,不会真去建连接
        if self._client is None:
            self._client = Anthropic(
                api_key=settings.anthropic_api_key,
                base_url=settings.anthropic_base_url,
            )
        return self._client

    def stream(self, messages: list, system: str, tools: list):
        """返回流式上下文管理器:text_stream 逐段吐增量,get_final_message 拿完整回复。"""
        return self.client.messages.stream(
            model=settings.model_id,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=8000,
        )

    def complete(self, messages: list, system: str, max_tokens: int = 1500):
        """非流式补全。记忆召回/提取这类"要 JSON 不要打字机"的内部调用用它。"""
        return self.client.messages.create(
            model=settings.model_id,
            system=system,
            messages=messages,
            max_tokens=max_tokens,
        )


llm = LLM()
