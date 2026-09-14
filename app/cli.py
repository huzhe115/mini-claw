"""CLI 渠道 — mini-claw 的第二扇门。

和网页共用同一个网关 HTTP 接口:浏览器聊的会话,命令行能接着聊(换门不换线)。
会话落库后,这条线重启服务也不断。

用法(在项目根目录):
  python -m app.cli            继续上次会话(没有则新建)
  python -m app.cli --new      新建会话
  python -m app.cli --list     列出会话手动选
  python -m app.cli --session <id>  进指定会话
"""
import argparse
import json
import sys
from pathlib import Path

import httpx

from app.config import settings

STATE_DIR = Path.home() / ".mini-claw"
STATE_FILE = STATE_DIR / "last_session"

DEFAULT_URL = "http://localhost:8000"

GRAY = "\033[90m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
RESET = "\033[0m"


def iter_sse_events(resp) -> iter:
    """把 SSE 响应流切成事件 dict(CLI 和测试共用)。"""
    buf = b""
    for chunk in resp.iter_bytes():
        buf += chunk
        while b"\n\n" in buf:
            frame, buf = buf.split(b"\n\n", 1)
            for line in frame.split(b"\n"):
                if line.startswith(b"data: "):
                    yield json.loads(line[6:].decode("utf-8"))


def read_last_session() -> str | None:
    try:
        return STATE_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def write_last_session(session_id: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(session_id, encoding="utf-8")


class ClawCLI:
    def __init__(self, base_url: str = DEFAULT_URL):
        self.base_url = base_url
        self._client = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=httpx.Timeout(300, connect=10),
                headers={"X-Gateway-Token": settings.gateway_token},
            )
        return self._client

    def api(self, method: str, path: str, **kwargs) -> httpx.Response:
        resp = self.client.request(method, path, **kwargs)
        if resp.status_code != 200:
            try:
                detail = resp.json().get("detail", "")
            except Exception:
                detail = ""
            raise RuntimeError(f"网关返回 {resp.status_code}: {detail}")
        return resp

    def list_sessions(self) -> list[dict]:
        return self.api("GET", "/api/sessions").json()

    def create_session(self) -> dict:
        return self.api("POST", "/api/sessions").json()

    def stream_chat(self, session_id: str, content: str) -> None:
        with self.client.stream(
            "POST", f"/api/sessions/{session_id}/chat/stream",
            json={"content": content},
        ) as resp:
            if resp.status_code != 200:
                try:
                    detail = resp.json().get("detail", "")
                except Exception:
                    detail = ""
                raise RuntimeError(f"网关返回 {resp.status_code}: {detail}")
            for ev in iter_sse_events(resp):
                self._print_event(ev)

    @staticmethod
    def _print_event(ev: dict) -> None:
        kind = ev["type"]
        if kind == "delta":
            print(ev["text"], end="", flush=True)  # 打字机
        elif kind == "tool":
            args = json.dumps(ev["input"], ensure_ascii=False)
            print(f"\n{YELLOW}[tool] {ev['name']}({args}){RESET}", flush=True)
        elif kind == "tool_result":
            print(f"{GRAY}[result] {ev['output'][:300]}{RESET}", flush=True)
        elif kind == "done":
            usage = ev.get("usage") or {}
            meta = ev.get("meta") or {}
            print(f"\n{GRAY}[tokens {usage.get('input_tokens')}/{usage.get('output_tokens')}"
                  f" · {meta.get('duration_ms')}ms]{RESET}")
        elif kind == "error":
            print(f"\n{RED}[error] {ev['detail']}{RESET}", flush=True)


def _resolve_session(cli: ClawCLI, args) -> str:
    """决定进哪个会话:指定 > 新建 > 手动选 > 上次 > 新建。"""
    if args.session:
        return args.session
    if args.new:
        return cli.create_session()["id"]
    if args.list:
        sessions = cli.list_sessions()
        if not sessions:
            print("还没有会话,新建一个")
            return cli.create_session()["id"]
        for i, s in enumerate(sessions):
            print(f"[{i}] {s['title']} ({s['id']})")
        choice = input(f"{CYAN}选一个会话(回车建新):{RESET} ").strip()
        if choice.isdigit() and int(choice) < len(sessions):
            return sessions[int(choice)]["id"]
        return cli.create_session()["id"]
    last = read_last_session()
    if last is not None:
        try:
            cli.api("GET", f"/api/sessions/{last}/messages")  # 验证还活着
            return last
        except RuntimeError:
            pass
    return cli.create_session()["id"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="mini-claw CLI 渠道 — 和网页共用同一个网关,换门不换线")
    parser.add_argument("--session", help="指定会话 id")
    parser.add_argument("--new", action="store_true", help="新建会话")
    parser.add_argument("--list", action="store_true", help="列出会话手动选择")
    parser.add_argument("--url", default=DEFAULT_URL, help="网关地址")
    args = parser.parse_args(argv)

    cli = ClawCLI(args.url)
    try:
        session_id = _resolve_session(cli, args)
    except RuntimeError as e:
        print(f"{RED}[error] {e}{RESET}")
        return 1

    write_last_session(session_id)
    print(f"{GRAY}[会话 {session_id}] 直接打字发消息,q 退出{RESET}")
    while True:
        try:
            text = input(f"{CYAN}你> {RESET}")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        text = text.strip()
        if text.lower() in ("q", "exit", ""):
            break
        try:
            cli.stream_chat(session_id, text)
        except RuntimeError as e:
            print(f"{RED}[error] {e}{RESET}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
