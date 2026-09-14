"""网关认证 — 对齐原版 OpenClaw gateway 的 token 模式。

Phase 1 用固定 token(配置在 .env),请求头 X-Gateway-Token 对上才放行。
以后要多用户再上 JWT(ai-chat-backend 里已有现成写法可搬)。
"""
from fastapi import Header, HTTPException

from app.config import settings


async def require_gateway_token(x_gateway_token: str | None = Header(default=None)) -> str:
    if not x_gateway_token or x_gateway_token != settings.gateway_token:
        raise HTTPException(status_code=401, detail="Invalid gateway token")
    return x_gateway_token
