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
    out = run_todo_write([
        {"content": "第一步", "status": "completed"},
        {"content": "第二步", "status": "in_progress"},
    ])
    assert "[x] 第一步" in out and "[>] 第二步" in out
    assert "(1/2 completed)" in out

    # 同时两个 in_progress 不允许
    assert "only one todo" in run_todo_write([
        {"content": "a", "status": "in_progress"},
        {"content": "b", "status": "in_progress"},
    ])
    TODO.items = []  # 还原,避免影响其他测试
