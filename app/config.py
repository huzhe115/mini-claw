from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """所有配置从环境变量 / .env 读取。"""

    model_config = SettingsConfigDict(env_file=".env")

    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.deepseek.com/anthropic"
    model_id: str = "deepseek-chat"

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/mini_claw"

    gateway_token: str = "dev-token"  # 网关认证 token,上线前必须改

    workspace_dir: str = "workspace"  # Agent 工具的活动范围,文件操作只允许在这之内
    max_steps: int = 15               # Agent 单次任务最多跑几轮,防死循环

    memory_dir: str = ""              # 记忆目录;空 = ~/.mini-claw/memory(和原版 OpenClaw 一致)
    memory_enabled: bool = True       # 测试里关掉,避免背景提取消耗假 LLM 脚本


settings = Settings()
