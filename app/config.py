import logging
import re
import secrets
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """所有配置从环境变量 / .env 读取。"""

    model_config = SettingsConfigDict(env_file=".env")

    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.deepseek.com/anthropic"
    model_id: str = "deepseek-chat"

    tavily_api_key: str = ""  # 联网搜索(web_search 工具);没配时报配置提示

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/mini_claw"

    gateway_token: str = "dev-token"  # 网关认证 token;没配时启动自动生成(见下方 bootstrap)

    workspace_dir: str = "workspace"  # Agent 工具的活动范围,文件操作只允许在这之内
    max_steps: int = 15  # Agent 单次任务最多跑几轮,防死循环
    # 排队等锁的秒数上限。必须盖住 wait 工具 300s 的钳制上限+一轮 LLM 时间,
    # 否则"倒计时任务"还没跑完排队就超时,用户会感觉消息发不出去。
    chat_lock_wait: int = 330

    memory_dir: str = ""  # 记忆目录;空 = ~/.mini-claw/memory(和原版 OpenClaw 一致)
    memory_enabled: bool = True  # 测试里关掉,避免背景提取消耗假 LLM 脚本

    redis_url: str = "redis://localhost:6379/0"
    rate_limit_per_minute: int = 30  # 每个 token 每分钟最多发多少条;<=0 关闭限流


def _bootstrap_gateway_token(env_path: Path = Path(".env")) -> str:
    """生成一把随机钥匙写进 .env(已有 GATEWAY_TOKEN= 行则替换,没有则追加)。

    对齐原版 OpenClaw 的 onboarding:向导自动生成 token,不逼用户手写。
    随机性来自 secrets.token_hex(32),64 位十六进制,猜不中。
    """
    token = secrets.token_hex(32)
    line = f"GATEWAY_TOKEN={token}"
    if env_path.exists():
        text = env_path.read_text(encoding="utf-8")
        new_text, n = re.subn(r"^GATEWAY_TOKEN=.*$", line, text, flags=re.MULTILINE)
        if n == 0:
            new_text = text.rstrip("\n") + "\n\n" + line
        env_path.write_text(new_text, encoding="utf-8")
    else:
        env_path.write_text(line, encoding="utf-8")
    return token


settings = Settings()

# 没配 token(.env 里为空或占位符)→ 自动生成并亮给用户看。
# 网页左下角填它;以后想看在 .env 里,或 python -c "from app.config import settings; print(settings.gateway_token)"
if not settings.gateway_token or settings.gateway_token == "dev-token":
    settings.gateway_token = _bootstrap_gateway_token()
    # 用 logging 不用 print:print 到重定向输出会被缓冲,启动日志里看不到
    logger.warning(
        "\n[mini-claw] 没找到 GATEWAY_TOKEN,已自动生成并写入 .env:\n\n  %s\n\n"
        "网页左下角填这串;想换钥匙改 .env 重启即可。\n",
        settings.gateway_token,
    )
