"""假 LLM — 按脚本出牌,让 Agent 循环测试不依赖网络。

内容块/用量用真实的 anthropic SDK 类型构造:和真机返回同一形状,
落库序列化(_dump/model_dump)走的就是生产路径。
用法:fake.script 里放每次 stream 调用的预设回复,调用时 pop 一个。
  {"chunks": ["你好"], "blocks": [块...], "usage": {...}, "error": 异常}
"""

from types import SimpleNamespace

from anthropic.types import TextBlock, ToolUseBlock, Usage


def text_block(text: str) -> TextBlock:
    return TextBlock(type="text", text=text)


def tool_use_block(name: str, input: dict, tool_id: str = "toolu_1") -> ToolUseBlock:
    return ToolUseBlock(type="tool_use", id=tool_id, name=name, input=input)


def usage(in_tokens: int, out_tokens: int) -> Usage:
    return Usage(input_tokens=in_tokens, output_tokens=out_tokens)


class FakeStream:
    def __init__(self, chunks, blocks, usage_obj, error):
        self.text_stream = [TextBlock(type="text", text=c) for c in chunks]
        self._blocks = blocks
        self._usage = usage_obj
        self._error = error

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get_final_message(self):
        if self._error:
            raise self._error
        return SimpleNamespace(content=self._blocks, usage=self._usage)


class FakeLLM:
    def __init__(self):
        self.script = []
        self.calls = []  # 每次 stream 的 (messages, system, tools) 留档,可断言

    def stream(self, messages, system, tools):
        # 存副本:主循环调用后还会继续往同一个列表追加消息,
        # 留引用的话断言时看到的是"后来"的状态
        self.calls.append({"messages": list(messages), "system": system, "tools": tools})
        item = self.script.pop(0)
        return FakeStream(
            item.get("chunks", []), item["blocks"], item.get("usage"), item.get("error")
        )
