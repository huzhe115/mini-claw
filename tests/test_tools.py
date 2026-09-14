"""工具层与安全闸门:路径逃逸、Windows bash 编码、危险命令拦截。"""
from app.agent.loop import _bash_blocked
from app.agent.tools import run_bash, run_read, run_write


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
