"""工具层 — 从 mini-claude/tools.py 迁移(Phase 1 只搬 3 个)。

三件套:
- TOOLS          schema 定义,告诉模型有哪些工具、参数长什么样
- run_* 函数      真正的执行逻辑(harness 干活,模型只生成意图)
- TOOL_HANDLERS  工具名 → 处理函数的查表分发

加一个工具 = 这里加三样东西,主循环一行不用改。
mini-claude 里还有 edit_file / glob / grep / todo_write,Phase 2+ 需要时原样搬来。
"""
import subprocess
from pathlib import Path

from app.config import settings

WORKDIR = Path(settings.workspace_dir).resolve()
MAX_TOOL_OUTPUT = 50_000  # 工具输出截断,省上下文

# ---------- 工具定义:告诉模型"我能做什么" ----------
TOOLS = [
    {"name": "bash", "description": "Run a shell command in the workspace.",
     "input_schema": {"type": "object",
                      "properties": {"command": {"type": "string"}},
                      "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "limit": {"type": "integer"}},
                      "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "content": {"type": "string"}},
                      "required": ["path", "content"]}},
]

# ---------- 文件工具 ----------


def safe_path(p: str) -> Path:
    """文件工具的唯一入口:解析后必须还在工作目录内,防止 ../ 逃逸。"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_read(path: str, limit: int | None = None) -> str:
    try:
        lines = safe_path(path).read_text(encoding="utf-8").splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    try:
        p = safe_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


# ---------- bash ----------
# Windows 坑(来自 mini-claude):cmd 输出可能是 GBK,命令前先 chcp 65001 切 UTF-8,
# 解码失败再回退 GBK,不要用 text=True 让 locale 猜——会乱码


def _decode_output(data: bytes) -> str:
    """统一按 UTF-8 解码(命令前已 chcp 65001),失败回退 GBK 兜底。"""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("gbk")
        except UnicodeDecodeError:
            return data.decode("utf-8", errors="replace")


def run_bash(command: str, timeout: int = 120) -> str:
    try:
        # check=False:命令失败不抛异常,退出码由 returncode 带回,拼进结果返回
        r = subprocess.run(f"chcp 65001 >nul & {command}", shell=True, cwd=WORKDIR,
                           capture_output=True, timeout=timeout, check=False)
        out = (_decode_output(r.stdout) + _decode_output(r.stderr)).strip()
        result = out[:MAX_TOOL_OUTPUT] if out else "(no output)"
        if r.returncode != 0:
            result += f"\n[exit code: {r.returncode}]"
        return result
    except subprocess.TimeoutExpired:
        return f"Error: timeout ({timeout}s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


# ---------- 分发表:工具名 → 处理函数 ----------
TOOL_HANDLERS = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
}
