"""网关路由 — 所有渠道的入口(Phase 3:WebChat + CLI + Cron)。

接口:
  POST /api/sessions                          新建会话
  GET  /api/sessions                          会话列表
  GET  /api/sessions/{id}/messages            历史消息(展示格式)
  POST /api/sessions/{id}/chat/stream         发消息,SSE 流式返回
  POST /api/cron                              建定时任务(排班表)
  GET  /api/cron                              定时任务列表
  DELETE /api/cron/{id}                       删定时任务
  POST /api/cron/{id}/run                     立即触发一次(演示/联调用)

流式桥接:Agent 循环是同步代码,跑在线程池里;
事件通过 asyncio.Queue 传回事件循环,再包成 SSE 帧推给前端。
Queue.put_nowait 是线程安全的,所以 sink 回调不需要任何锁。
"""
import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cron import _fire_cron, parse_cron, schedule, unschedule
from app.core.sessions import store
from app.core.turn import run_turn
from app.db import get_db
from app.models import CronJobModel, SessionModel
from app.rate_limit import check_rate_limit

from .auth import require_gateway_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["gateway"])


def _sse(data: dict) -> str:
    """把一个事件包成 SSE 帧(空行是事件边界)。"""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


class ChatIn(BaseModel):
    content: str = Field(min_length=1, max_length=10000)


class CronIn(BaseModel):
    session_id: str
    prompt: str = Field(min_length=1, max_length=10000)
    schedule: str = Field(min_length=1, max_length=100)


def _job_dict(job: CronJobModel) -> dict:
    return {"id": job.id, "session_id": job.session_id, "prompt": job.prompt,
            "schedule": job.schedule, "enabled": job.enabled,
            "last_run_at": job.last_run_at, "created_at": job.created_at}


@router.post("/sessions", status_code=201)
async def create_session(db: AsyncSession = Depends(get_db),
                         _: str = Depends(require_gateway_token)):
    s = await store.create(db)
    return {"id": s.id, "title": s.title, "created_at": s.created_at}


@router.get("/sessions")
async def list_sessions(db: AsyncSession = Depends(get_db),
                        _: str = Depends(require_gateway_token)):
    return [{"id": s.id, "title": s.title, "created_at": s.created_at}
            for s in await store.list(db)]


@router.get("/sessions/{session_id}/messages")
async def list_messages(session_id: str, db: AsyncSession = Depends(get_db),
                        _: str = Depends(require_gateway_token)):
    s = await store.get(db, session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    return s.display


@router.post("/sessions/{session_id}/chat/stream")
async def chat_stream(session_id: str, body: ChatIn,
                      db: AsyncSession = Depends(get_db),
                      token: str = Depends(require_gateway_token)):
    if not await check_rate_limit(token):
        raise HTTPException(status_code=429, detail="请求太频繁,休息一分钟")
    s = await store.get(db, session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    if s.lock.locked():
        raise HTTPException(status_code=409, detail="该会话正在处理上一条消息,请稍候")

    async with s.lock:
        queue: asyncio.Queue = asyncio.Queue()

        def sink(ev) -> None:
            # done 事件不外发(收尾帧由 event_gen 组装);None 哨兵必须转发,
            # event_gen 靠它知道流结束了
            if ev is None or ev["type"] != "done":
                queue.put_nowait(ev)

        async def event_gen():
            yield _sse({"type": "user_msg",
                        "message": {"role": "user", "content": body.content}})
            task = asyncio.create_task(run_turn(db, s, body.content, sink=sink))
            while True:
                ev = await queue.get()
                if ev is None:
                    break
                yield _sse(ev)
            result = await task
            if result.get("error"):
                return  # error 事件已经转发过了
            yield _sse({"type": "done", "usage": result["usage"],
                        "message": {"role": "assistant", "steps": result["steps"]},
                        "meta": result["meta"]})

        # ponytail: 客户端中途断连时,Agent 线程会继续把任务跑完(结果照常进会话),
        # 只是没人接收事件。个人项目可接受,任务队列化时统一治理。
        return StreamingResponse(event_gen(), media_type="text/event-stream")


# ---------- 排班表(cron) ----------

@router.post("/cron", status_code=201)
async def create_cron(body: CronIn, db: AsyncSession = Depends(get_db),
                      _: str = Depends(require_gateway_token)):
    if await db.get(SessionModel, body.session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")
    try:
        parse_cron(body.schedule)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"bad cron expression: {e}")

    job = CronJobModel(session_id=body.session_id, prompt=body.prompt,
                       schedule=body.schedule)
    db.add(job)
    await db.commit()
    await db.refresh(job)
    schedule(job.id, body.schedule)  # 挂上报时员
    return _job_dict(job)


@router.get("/cron")
async def list_cron(db: AsyncSession = Depends(get_db),
                    _: str = Depends(require_gateway_token)):
    jobs = (await db.scalars(select(CronJobModel).order_by(CronJobModel.id))).all()
    return [_job_dict(j) for j in jobs]


@router.delete("/cron/{job_id}", status_code=204)
async def delete_cron(job_id: int, db: AsyncSession = Depends(get_db),
                      _: str = Depends(require_gateway_token)):
    job = await db.get(CronJobModel, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="cron job not found")
    unschedule(job_id)
    await db.delete(job)
    await db.commit()


@router.post("/cron/{job_id}/run")
async def run_cron_now(job_id: int, _: str = Depends(require_gateway_token)):
    """立即触发一次(演示/联调用),等价于报时员到点敲门。"""
    return await _fire_cron(job_id)
