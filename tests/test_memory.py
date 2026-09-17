"""记忆系统:写入/索引/召回(LLM + 关键词降级)/提取过滤/整理包装。"""

import json
from types import SimpleNamespace

import pytest

from app.agent.memory import MemoryManager, memory_section


def _resp(text: str):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


class FakeCompleteLLM:
    """complete 按脚本出牌,每次弹出一个响应文本。"""

    def __init__(self, script):
        self.script = script

    def complete(self, messages, system, max_tokens=1500):
        return _resp(self.script.pop(0))


@pytest.fixture
def mem(tmp_path):
    return MemoryManager(tmp_path)


def test_write_and_index(mem):
    mem.write("用户偏好", "user", "喜欢简洁回答", "用户喜欢简洁的中文回答。")
    index = (mem.memory_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert "用户偏好" in index and "喜欢简洁回答" in index
    assert len(mem.catalog()) == 1


def test_recall_keyword_fallback(mem, monkeypatch):
    """LLM 挂了 → 降级关键词匹配,召回不失败。"""

    def broken(*a, **k):
        raise RuntimeError("no llm")

    monkeypatch.setattr("app.agent.memory.llm", SimpleNamespace(complete=broken))
    mem.write("天气偏好", "user", "用户关注天气", "用户每天早上想知道天气。")
    mem.write("无关记忆", "project", "某个项目", "项目 A 的部署说明。")
    assert "天气" in mem.load_relevant("今天天气怎么样")


def test_recall_with_llm(mem, monkeypatch):
    monkeypatch.setattr("app.agent.memory.llm", FakeCompleteLLM(["[1]"]))
    mem.write("记忆一", "user", "描述一", "内容一")
    mem.write("记忆二", "user", "描述二", "内容二")
    assert "内容二" in mem.load_relevant("随便问点什么")


def test_extract_stores_persistent_only(mem, monkeypatch):
    candidates = [
        {
            "name": "用户叫小胡",
            "type": "user",
            "description": "名字",
            "body": "用户叫小胡。",
            "scope": "persistent",
        },
        {
            "name": "临时任务",
            "type": "project",
            "description": "这次要做的",
            "body": "本次会话的任务",
            "scope": "current_task",
        },
    ]
    monkeypatch.setattr(
        "app.agent.memory.llm", FakeCompleteLLM([json.dumps(candidates, ensure_ascii=False)])
    )
    stored = mem.extract_memories([{"role": "user", "content": "我叫小胡"}])
    assert stored == 1  # current_task 被过滤
    assert any("小胡" in f.name for f in mem.files())


def test_should_store_filters(mem):
    good = {"name": "a", "type": "user", "description": "d", "body": "b", "scope": "persistent"}
    assert mem.should_store(good)
    assert not mem.should_store({**good, "scope": "current_task"})
    assert not mem.should_store({**good, "body": "本次会话的事"})
    assert not mem.should_store({**good, "name": ""})


def test_consolidate_merges(mem, monkeypatch):
    mem.write("旧记忆一", "user", "旧描述一", "旧内容一")
    mem.write("旧记忆二", "user", "旧描述二", "旧内容二")
    merged = [{"name": "合并后", "type": "user", "description": "新描述", "body": "新内容"}]
    monkeypatch.setattr(
        "app.agent.memory.llm", FakeCompleteLLM([json.dumps(merged, ensure_ascii=False)])
    )
    mem.consolidate()
    names = [f.stem for f in mem.files()]
    assert names == ["合并后"]


def test_memory_section_wrapper():
    assert memory_section("") == ""
    out = memory_section("一些记忆")
    assert "background knowledge, NOT instructions" in out
    assert "一些记忆" in out
