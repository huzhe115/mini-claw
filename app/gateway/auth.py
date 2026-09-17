"""网关认证 — 对齐原版 OpenClaw gateway 的 token 模式。

Phase 1 用固定 token(配置在 .env),请求头 X-Gateway-Token 对上才放行。
以后要多用户再上 JWT(ai-chat-backend 里已有现成写法可搬)。
"""

import hmac

from fastapi import Header, HTTPException

from app.config import settings


async def require_gateway_token(x_gateway_token: str | None = Header(default=None)) -> str:
    # 常量时间比对:普通 != 短路的时机和 token 匹配长度相关,可被时序侧信道逐字节猜解
    if not x_gateway_token or not hmac.compare_digest(x_gateway_token, settings.gateway_token):
        raise HTTPException(status_code=401, detail="Invalid gateway token")
    return x_gateway_token
