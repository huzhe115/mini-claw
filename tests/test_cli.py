"""CLI 渠道的纯逻辑测试:SSE 解析、会话指针文件、事件打印。"""

from types import SimpleNamespace

from app import cli


def _fake_resp(chunks: list[bytes]):
    """模拟 httpx 流式响应,iter_bytes 分块吐出。"""

    def iter_bytes():
        yield from chunks

    return SimpleNamespace(iter_bytes=iter_bytes)


def test_iter_sse_events():
    frames = (
        b'data: {"type":"delta","text":"\xe4\xbd\xa0"}\n\n'
        b'data: {"type":"delta","text":"\xe5\xa5\xbd"}\n\n'
        b'data: {"type":"done","usage":{"input_tokens":1,"output_tokens":2}}\n\n'
    )
    # 故意按不规则块切分,验证跨块拼接
    resp = _fake_resp([frames[:10], frames[10:50], frames[50:]])
    events = list(cli.iter_sse_events(resp))
    assert [e["type"] for e in events] == ["delta", "delta", "done"]
    assert events[0]["text"] + events[1]["text"] == "你好"


def test_last_session_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "STATE_FILE", tmp_path / "last_session")
    assert cli.read_last_session() is None
    cli.write_last_session("abc123")
    assert cli.read_last_session() == "abc123"


def test_print_event_delta(capsys):
    cli.ClawCLI._print_event({"type": "delta", "text": "你好"})
    assert "你好" in capsys.readouterr().out


def test_print_event_tool(capsys):
    cli.ClawCLI._print_event({"type": "tool", "name": "bash", "input": {"command": "echo hi"}})
    out = capsys.readouterr().out
    assert "[tool] bash" in out and "echo hi" in out
