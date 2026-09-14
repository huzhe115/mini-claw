"""假 LLM — 按脚本出牌,让 Agent 循环测试不依赖网络。

用法:fake.script 里放每次 stream 调用的预设回复,调用时 pop 一个。
  {"chunks": ["你好"], "blocks": [块...], "usage": {...}, "error": 异常}
"""
from types import SimpleNamespace


def text_block(text: str):
    return SimpleNamespace(type="text", text=text)


def tool_use_block(name: str, input: dict, tool_id: str = "toolu_1"):
    return SimpleNamespace(type="tool_use", name=name, input=input, id=tool_id)


def usage(in_tokens: int, out_tokens: int):
    return SimpleNamespace(input_tokens=in_tokens, output_tokens=out_tokens)


class FakeStream:
    def __init__(self, chunks, blocks, usage_obj, error):
        self.text_stream = [SimpleNamespace(text=c) for c in chunks]
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
        self.calls.append({"messages": messages, "system": system, "tools": tools})
        item = self.script.pop(0)
        return FakeStream(item.get("chunks", []), item["blocks"],
                          item.get("usage"), item.get("error"))
