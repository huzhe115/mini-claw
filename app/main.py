from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.gateway import router as gateway_router

# Agent 的工作目录,启动时确保存在
Path(settings.workspace_dir).mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="OpenClaw",
    version="0.1.0",
    description="从 0 到 1 写一个 OpenClaw — 常驻私人 AI 助手",
)
app.include_router(gateway_router.router)

# 简易前端:浏览器打开 http://localhost:8000/ 就是聊天页
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse("static/index.html")
