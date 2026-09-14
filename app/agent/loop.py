"""Agent 核心循环 — 从 mini-claude/main.py 迁移(Phase 1 精简版)。

只保留 ReAct 主干:调模型 → 有工具调用就执行回填 → 再调模型,直到模型只说话不调工具。
和 mini-claude 的差异:
- 输出从 print 改成事件回调 sink:每个增量/工具动作发一个事件,
  网关层据此转发给前端(流式打字机)和记录展示步骤。CLI 渠道以后也能复用同一回调。
- 权限闸门只留硬拒绝表(web 场景没有交互式确认,危险命令默认拒绝)。
- 上下文压缩 / 记忆 / hooks / 子代理 / MCP 都在 mini-claude 里,Phase 3 按需移植。
"""
import os
import re
import time

from app.config import settings

from . import tools as T
from .llm import llm

SYSTEM = (
    "You are OpenClaw, a personal AI assistant that lives in a backend service. "
    "Help the user with questions and tasks, using tools to read files, run commands "
    "and write files when needed. "
    "Always reply in the language the user uses. "
    "For multi-step tasks, plan first; only call tools when you actually need them. "
    f"Your workspace is {T.WORKDIR}. "
    + ("The bash tool runs Windows cmd.exe — use Windows command syntax. "
       if os.name == "nt" else "")
)

# ---------- 权限闸门:硬拒绝表(web 场景默认拒绝,无交互确认) ----------
# ponytail: 这不是完整安全边界——真边界要靠 OS 级沙箱/容器,Phase 3 做工具权限配置
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "format c:"]
DESTRUCTIVE_WORD = re.compile(r"(?i)(?:^|[;&|()\n])\s*(?:rm|del)(?=\s|$|[;&|()])")


def _bash_blocked(command: str) -> str | None:
    for pattern in DENY_LIST:
        if pattern in command.lower():
            return f"Blocked by deny list: '{pattern}'"
    if DESTRUCTIVE_WORD.search(command):
        return "Blocked by safety policy: destructive commands are denied in web mode."
    return None


def call_llm(messages: list, system: str, tools: list, on_text, retries: int = 2):
    """调 LLM。限流/过载/网络错误指数退避重试;增量文本通过 on_text 逐段发出去。"""
    for attempt in range(retries + 1):
        try:
            with llm.stream(messages=messages, system=system, tools=tools) as s:
                for block in s.text_stream:
                    text = getattr(block, "text", None) or ""
                    if text:
                        on_text(text)
                return s.get_final_message()
        except Exception as e:
            msg = str(e).lower()
            retriable = any(k in msg for k in ("429", "529", "timeout", "connection",
                                               "rate limit", "overloaded", "remote"))
            if retriable and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise


def agent_loop(messages: list, system: str = SYSTEM, tools: list | None = None,
               max_steps: int | None = None, sink=None) -> None:
    """核心循环。messages 原地追加(调用方持引用,失败回滚由调用方做)。

    sink 收到的事件:
      {"type": "delta", "text": ...}                      增量文本
      {"type": "tool", "name": ..., "input": ...}         模型发起的工具调用
      {"type": "tool_result", "name": ..., "output": ...} 工具执行结果
      {"type": "done", "usage": {...}, "stopped": bool}   结束(正常/步数耗尽)
    """
    emit = sink or (lambda ev: None)
    tools = tools if tools is not None else T.TOOLS
    max_steps = max_steps or settings.max_steps
    total_in = total_out = 0  # 会话累计 token

    for _ in range(max_steps):
        # 兜底:正常流式会逐段吐 delta;个别端点/假实现不流式时,
        # 若这轮一个增量都没有,就从最终回复的文本块补发一次,展示层不至于丢内容
        emitted_text = False

        def on_text(t: str) -> None:
            nonlocal emitted_text
            emitted_text = True
            emit({"type": "delta", "text": t})

        response = call_llm(messages, system, tools, on_text=on_text)
        if not emitted_text:
            full = "".join(getattr(b, "text", "") for b in response.content
                           if getattr(b, "type", "") == "text")
            if full:
                emit({"type": "delta", "text": full})
        usage = getattr(response, "usage", None)  # 部分端点不回报 usage
        if usage is not None:
            total_in += getattr(usage, "input_tokens", 0)
            total_out += getattr(usage, "output_tokens", 0)
        messages.append({"role": "assistant", "content": response.content})

        tool_calls = [b for b in response.content if b.type == "tool_use"]
        if not tool_calls:
            # 模型只说话不调工具 = 任务完成
            emit({"type": "done", "usage": {"input_tokens": total_in,
                                            "output_tokens": total_out}, "stopped": False})
            return

        results = []
        for block in tool_calls:
            emit({"type": "tool", "name": block.name, "input": block.input})
            if block.name == "bash":
                blocked = _bash_blocked(block.input.get("command", ""))
                output = blocked if blocked else None
            else:
                output = None
            if output is None:
                handler = T.TOOL_HANDLERS.get(block.name)
                try:
                    output = handler(**block.input) if handler else f"Error: unknown tool {block.name}"
                except Exception as e:
                    output = f"Error: {e}"
            output = str(output)[:T.MAX_TOOL_OUTPUT]
            emit({"type": "tool_result", "name": block.name, "output": output})
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output,
            })
        messages.append({"role": "user", "content": results})

    emit({"type": "done", "usage": {"input_tokens": total_in,
                                    "output_tokens": total_out}, "stopped": True})
