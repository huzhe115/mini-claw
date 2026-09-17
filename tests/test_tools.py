"""工具层与安全闸门:路径逃逸、Windows bash 编码、危险命令拦截、Phase 3 新工具。"""

from app.agent.loop import _bash_blocked
from app.agent.tools import (
    TODO,
    run_bash,
    run_edit,
    run_glob,
    run_grep,
    run_read,
    run_todo_write,
    run_wait,
    run_web_search,
    run_write,
)


def test_path_escape_blocked():
    out = run_read("../secret.txt")
    assert "escapes workspace" in out


def test_write_read_roundtrip():
    assert "Wrote" in run_write("hello.txt", "hi mini-claw")
    assert run_read("hello.txt") == "hi mini-claw"


def test_bash_echo():
    out = run_bash("echo hello")
    assert "hello" in out


def test_bash_deny_list():
    assert _bash_blocked("echo hello") is None
    assert "deny list" in _bash_blocked("sudo rm -rf /")
    assert "safety policy" in _bash_blocked("del /f important.txt")


def test_edit_replaces_once():
    run_write("edit.txt", "aa bb aa")
    assert "Edited" in run_edit("edit.txt", "aa", "cc")
    assert run_read("edit.txt") == "cc bb aa"  # 只替换第一处


def test_edit_missing_text():
    run_write("edit.txt", "hello")
    assert "text not found" in run_edit("edit.txt", "nope", "x")


def test_glob_finds_files():
    run_write("sub/inner.txt", "x")
    out = run_glob("**/*.txt")
    assert "sub/inner.txt" in out


def test_grep_finds_lines():
    run_write("g.txt", "line one\nneedle here\n")
    out = run_grep("needle")
    assert "g.txt:2: needle here" in out


def test_grep_bad_regex():
    assert "bad regex" in run_grep("([unclosed")


def test_todo_write_validate_and_render():
    out = run_todo_write(
        [
            {"content": "第一步", "status": "completed"},
            {"content": "第二步", "status": "in_progress"},
        ]
    )
    assert "[x] 第一步" in out and "[>] 第二步" in out
    assert "(1/2 completed)" in out

    # 同时两个 in_progress 不允许
    assert "only one todo" in run_todo_write(
        [
            {"content": "a", "status": "in_progress"},
            {"content": "b", "status": "in_progress"},
        ]
    )
    TODO.items = []  # 还原,避免影响其他测试


def test_wait_really_sleeps_clamped(monkeypatch):
    slept = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))
    assert run_wait(2) == "Waited 2 seconds"
    assert slept == [2]
    # 超上限钳制到 24h:提醒队列不占锁,长等待没危害;防无限大即可
    assert run_wait(99999) == "Waited 86400 seconds"
    assert slept == [2, 86400]


def test_web_search_no_key(monkeypatch):
    monkeypatch.setattr("app.agent.tools.settings.tavily_api_key", "")
    assert "TAVILY_API_KEY" in run_web_search("hello")


def test_web_search_formats_results(monkeypatch):
    monkeypatch.setattr("app.agent.tools.settings.tavily_api_key", "tvly-test")

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "results": [
                    {"title": "结果一", "url": "https://a.com/1", "content": "第一段 摘要内容"},
                    {"title": "结果二", "url": "https://b.com/2", "content": "第二段 摘要内容"},
                ]
            }

    calls = []
    monkeypatch.setattr(
        "app.agent.tools.httpx.post", lambda *a, **kw: calls.append(kw) or FakeResp()
    )
    out = run_web_search("测试查询", max_results=99)  # 超上限钳到 10
    assert calls[0]["json"]["max_results"] == 10
    assert "1. 结果一" in out and "https://a.com/1" in out
    assert "2. 结果二" in out
    assert "第一段 摘要内容" in out


def test_web_search_http_error(monkeypatch):
    monkeypatch.setattr("app.agent.tools.settings.tavily_api_key", "tvly-test")

    class FakeResp:
        def raise_for_status(self):
            raise RuntimeError("401 unauthorized")

    monkeypatch.setattr("app.agent.tools.httpx.post", lambda *a, **kw: FakeResp())
    assert "401" in run_web_search("hello")


def test_delay_until_parses(monkeypatch):
    from datetime import datetime as real_dt

    from app.agent.tools import _delay_until

    fake = type("FakeDT", (), {"now": staticmethod(lambda: real_dt(2026, 9, 17, 17, 0, 0))})
    monkeypatch.setattr("app.agent.tools.datetime", fake)
    assert _delay_until("17:16") == 16 * 60
    assert _delay_until("9:05") == -28500  # 已过,负数
    assert _delay_until("五点十六") is None  # 非法格式


def test_wait_at_time_queues_reminder(monkeypatch):
    from datetime import datetime as real_dt

    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from app.agent.tools import run_wait
    from app.core.cron import REMINDERS, list_reminders
    from app.core.sessions import session_ctx

    fake = type("FakeDT", (), {"now": staticmethod(lambda: real_dt(2026, 9, 17, 17, 0, 0))})
    monkeypatch.setattr("app.agent.tools.datetime", fake)
    monkeypatch.setattr("app.core.cron.scheduler", AsyncIOScheduler())
    REMINDERS.clear()  # 其它测试可能注册过提醒,全局字典先清空
    token = session_ctx.set("sess-1")
    try:
        assert "queued" in run_wait(at_time="17:16", message="喝水")
        items = list_reminders()
        assert len(items) == 1 and items[0]["message"] == "喝水"
        # 已过的时间:报错让模型跟用户确认
        assert "already passed" in run_wait(at_time="09:00", message="喝水")
    finally:
        session_ctx.reset(token)
