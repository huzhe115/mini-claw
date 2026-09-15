from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.core.cron import init_scheduler, shutdown_scheduler
from app.gateway import router as gateway_router

# Agent 的工作目录,启动时确保存在
Path(settings.workspace_dir).mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 报时员随服务一起上班下班,排班表从库里恢复
    await init_scheduler()
    yield
    shutdown_scheduler()


app = FastAPI(
    title="Mini-Claw",
    version="0.4.0",
    description="从 0 到 1 复刻 OpenClaw — 常驻私人 AI 助手",
    lifespan=lifespan,
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
