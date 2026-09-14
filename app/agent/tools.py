"""工具层 — 从 mini-claude/tools.py 迁移(Phase 1 搬 3 个,Phase 3 补齐 4 个)。

三件套:
- TOOLS          schema 定义,告诉模型有哪些工具、参数长什么样
- run_* 函数      真正的执行逻辑(harness 干活,模型只生成意图)
- TOOL_HANDLERS  工具名 → 处理函数的查表分发

加一个工具 = 这里加三样东西,主循环一行不用改。
"""
import json
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
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"},
                                     "old_text": {"type": "string"},
                                     "new_text": {"type": "string"}},
                      "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern; ** matches recursively.",
     "input_schema": {"type": "object",
                      "properties": {"pattern": {"type": "string"}},
                      "required": ["pattern"]}},
    {"name": "grep", "description": "Search file contents with a regex; glob_pattern filters files.",
     "input_schema": {"type": "object",
                      "properties": {"pattern": {"type": "string"},
                                     "glob_pattern": {"type": "string"}},
                      "required": ["pattern"]}},
    {"name": "todo_write", "description": "Create and manage a task list. Plan multi-step tasks first, update statuses as you go.",
     "input_schema": {"type": "object",
                      "properties": {"todos": {"type": "array", "maxItems": 20,
                                               "items": {"type": "object",
                                                         "properties": {"content": {"type": "string"},
                                                                        "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}},
                                                         "required": ["content", "status"]}}},
                      "required": ["todos"]}},
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


def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        p = safe_path(path)
        text = p.read_text(encoding="utf-8")
        if old_text not in text:
            return f"Error: text not found in {path}"
        p.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


def run_glob(pattern: str) -> str:
    import glob as g
    try:
        matches = sorted({
            # Windows 的 glob 返回反斜杠路径,统一成 / 再给模型看
            m.replace("\\", "/")
            for m in g.glob(pattern, root_dir=WORKDIR, recursive=True)
            if (WORKDIR / m).resolve().is_relative_to(WORKDIR)
        })
        shown = matches[:200]
        if len(matches) > 200:
            shown.append("... (more matches omitted; narrow the pattern)")
        return "\n".join(shown) if shown else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


# ponytail: 纯 Python 正则扫描,上限 200 个文件、200 条结果,够用再升级
def run_grep(pattern: str, glob_pattern: str = "**/*") -> str:
    import re
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"Error: bad regex: {e}"
    out, files_seen = [], 0
    for p in sorted(WORKDIR.glob(glob_pattern)):
        if {".venv", ".git", "__pycache__"} & set(p.parts):
            continue
        if not p.is_file() or files_seen >= 200:
            break
        files_seen += 1
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                out.append(f"{p.relative_to(WORKDIR)}:{i}: {line.strip()}")
                if len(out) >= 200:
                    return "\n".join(out) + "\n...(more matches omitted)"
    return "\n".join(out) if out else "(no matches)"


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


# ---------- todo_write:给模型一个"钉住目标"的计划工具 ----------
# 来自 mini-claude s05。todo_write 不增加执行能力,只增加规划能力:
# 长任务里上下文越滚越长,早期目标被稀释,todo 把目标钉在状态里。
# ponytail: 全局单例,所有会话共享一份待办——私人助手场景下反而合理。

class TodoManager:
    def __init__(self):
        self.items = []

    def update(self, todos) -> str:
        if isinstance(todos, str):  # 模型偶尔会传 JSON 字符串
            todos = json.loads(todos)
        if not isinstance(todos, list):
            raise TypeError("todos must be a list")
        if len(todos) > 20:
            raise ValueError("max 20 todos")
        validated, in_progress = [], 0
        for i, t in enumerate(todos):
            content = str(t.get("content", "")).strip()
            status = str(t.get("status", "pending")).lower()
            if not content:
                raise ValueError(f"todos[{i}] needs content")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"todos[{i}] bad status: {status}")
            in_progress += status == "in_progress"
            validated.append({"content": content, "status": status})
        if in_progress > 1:
            raise ValueError("only one todo in_progress at a time")
        self.items = validated
        return self.render()

    def render(self) -> str:
        if not self.items:
            return "No todos."
        marks = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
        lines = [f"{marks[t['status']]} {t['content']}" for t in self.items]
        done = sum(t["status"] == "completed" for t in self.items)
        lines.append(f"\n({done}/{len(self.items)} completed)")
        return "\n".join(lines)


TODO = TodoManager()


def run_todo_write(todos) -> str:
    try:
        return TODO.update(todos)
    except (ValueError, TypeError, json.JSONDecodeError) as e:
        return f"Error: {e}"


# ---------- 分发表:工具名 → 处理函数 ----------
TOOL_HANDLERS = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
    "grep": run_grep,
    "todo_write": run_todo_write,
}
