"""Cron 定时任务 — 报时员:到点自动替用户发起一次 Agent 运行。

排班表在 cron_jobs 表;APScheduler(AsyncIOScheduler)是每秒看一眼排班表的报时员。
到点 → 找到目标会话 → 以 role="cron" 注入消息 → Agent 正常跑一轮 → 结果留在会话里,
用户打开网页/CLI 就能看到"它主动来找你了"。
这就是 mini-claw 和 mini-claude 的本质区别:mini-claude 你不开它它就不在,
没人看表;mini-claw 常驻,报时员一直在。
"""
import logging
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app.core.sessions import store
from app.core.turn import run_turn
from app.db import async_session
from app.models import CronJobModel, SessionModel

logger = logging.getLogger(__name__)

scheduler: AsyncIOScheduler | None = None  # 测试环境不启动(ASGITransport 不触发 lifespan)


def parse_cron(expression: str) -> CronTrigger:
    """校验并解析 cron 表达式,非法时抛 ValueError。"""
    return CronTrigger.from_crontab(expression)


async def init_scheduler() -> None:
    """启动报时员,并把库里启用的排班挂上去。"""
    global scheduler
    if scheduler is not None:
        return
    scheduler = AsyncIOScheduler()
    scheduler.start()
    async with async_session() as db:
        jobs = (await db.scalars(
            select(CronJobModel).where(CronJobModel.enabled)
        )).all()
    for job in jobs:
        try:
            trigger = parse_cron(job.schedule)
        except ValueError:
            logger.warning("cron job %s 表达式非法,跳过: %s", job.id, job.schedule)
            continue
        scheduler.add_job(_fire_cron, trigger, args=[job.id], id=f"cron_{job.id}")
    logger.info("cron scheduler started, %d job(s) registered", len(jobs))


def shutdown_scheduler() -> None:
    global scheduler
    if scheduler is not None:
        scheduler.shutdown(wait=False)
        scheduler = None


def schedule(job_id: int, expression: str) -> None:
    """把任务挂到报时员(测试环境 scheduler 为 None 时跳过)。"""
    if scheduler is not None:
        scheduler.add_job(_fire_cron, parse_cron(expression),
                          args=[job_id], id=f"cron_{job_id}")


def unschedule(job_id: int) -> None:
    if scheduler is not None:
        try:
            scheduler.remove_job(f"cron_{job_id}")
        except Exception as e:  # 任务不存在等,不碍事
            logger.debug("unschedule %s failed: %s", job_id, e)


async def _fire_cron(job_id: int) -> dict:
    """报时员敲门:替用户发起一次 Agent 运行。返回运行摘要或 {"skipped": 原因}。"""
    async with async_session() as db:
        job = await db.get(CronJobModel, job_id)
        if job is None or not job.enabled:
            return {"skipped": "job missing or disabled"}
        if await db.get(SessionModel, job.session_id) is None:
            return {"skipped": "session not found"}
        session = await store.get(db, job.session_id)
        if session.lock.locked():
            return {"skipped": "session busy"}  # 用户正在聊,这次不插话
        async with session.lock:
            result = await run_turn(db, session, job.prompt, role="cron")
        if result.get("error"):
            logger.warning("cron job %s 运行失败: %s", job_id, result["error"])
            return result
        job.last_run_at = datetime.now(UTC)
        await db.commit()
        return result
