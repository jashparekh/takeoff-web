"""The background worker (arq on Redis, plan decision D2). Run it with:

    arq app.pipeline.jobs.WorkerSettings

Each task opens its own database session and calls a stage in `stages.py`. Failures retry up to 3 times with
backoff; a page that still fails is marked `needs_attention` and never holds up the other pages.
"""
from __future__ import annotations
import logging
from datetime import timedelta

from arq import Retry, cron
from arq.connections import RedisSettings
from sqlalchemy import select

from ..db.models import Job, Sheet, Upload
from ..db.session import session_factory
from ..settings import get_settings
from ..storage import get_storage
from . import stages

log = logging.getLogger('takeoff.worker')
MAX_TRIES = 3
STALE = timedelta(minutes=2)
JOB_TIMEOUT = 300                                          # seconds per stage run


def queue_redis() -> RedisSettings:
    url = get_settings().redis_url
    if not url:
        raise RuntimeError('REDIS_URL is not set (see .env.example)')
    return RedisSettings.from_dsn(url)


async def run(ctx, fn, subject):
    """Run a sync stage in a thread; on failure retry with backoff (10 s, 20 s) until the last try."""
    final = ctx['job_try'] >= MAX_TRIES
    try:
        with session_factory()() as db:
            return await _thread(fn, db, get_storage(), subject, final=final)
    except Exception as e:
        if final:
            log.exception('%s %s failed for good', fn.__name__, subject)
            return None
        log.warning('%s %s failed (try %s): %s', fn.__name__, subject, ctx['job_try'], e)
        raise Retry(defer=10 * ctx['job_try']) from e


async def _thread(fn, *args, **kw):
    import anyio
    return await anyio.to_thread.run_sync(lambda: fn(*args, **kw))


async def ingest(ctx, upload_id: str):
    new = await run(ctx, stages.ingest, upload_id) or []
    for sid in new:
        await ctx['redis'].enqueue_job('render', str(sid), _job_id=f'render:{sid}')


async def render(ctx, sheet_id: str):
    await run(ctx, stages.render, sheet_id)


def stale_work(db, at) -> tuple[list, list]:
    """Uploads and sheets to queue again: left `queued` for over 2 minutes, or whose job has been `running` longer
    than a job may take (the worker died mid-job)."""
    cutoff, dead = at - STALE, at - timedelta(seconds=JOB_TIMEOUT + 60)
    ups = set(db.scalars(select(Upload.id).where(Upload.status == 'queued', Upload.created_at < cutoff)))
    shs = set(db.scalars(select(Sheet.id).where(Sheet.status == 'queued', Sheet.created_at < cutoff)))
    for j in db.scalars(select(Job).where(Job.status == 'running', Job.started_at < dead)):
        (ups if j.stage == 'ingest' else shs).add(j.upload_id if j.stage == 'ingest' else j.sheet_id)
    return sorted(ups, key=str), sorted(shs, key=str)


async def requeue_stale(ctx):
    """Safety net, every minute. arq job ids make this a no-op for jobs still waiting in Redis."""
    with session_factory()() as db:
        ups, shs = stale_work(db, stages.now())
    for u in ups:
        await ctx['redis'].enqueue_job('ingest', str(u), _job_id=f'ingest:{u}')
    for s in shs:
        await ctx['redis'].enqueue_job('render', str(s), _job_id=f'render:{s}')
    if ups or shs:
        log.info('requeued %d uploads, %d sheets', len(ups), len(shs))


async def startup(ctx):
    lg = logging.getLogger('takeoff')                     # arq sets up its own logger; add ours alongside
    if not lg.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s', '%H:%M:%S'))
        lg.addHandler(h)
        lg.setLevel(logging.INFO)


class WorkerSettings:
    functions = [ingest, render]
    cron_jobs = [cron(requeue_stale, second=0)]            # every minute
    on_startup = startup
    max_tries = MAX_TRIES
    job_timeout = JOB_TIMEOUT
    max_jobs = 2                                           # one page renders at a time per job; keep memory bounded
    keep_result = 60                                       # short, so a job id can be queued again soon
    redis_settings = queue_redis() if get_settings().redis_url else RedisSettings()


async def enqueue_ingest(upload_id) -> None:
    """Called by the web process after the upload row is committed."""
    from arq import create_pool
    pool = await create_pool(queue_redis())
    try:
        await pool.enqueue_job('ingest', str(upload_id), _job_id=f'ingest:{upload_id}')
    finally:
        await pool.aclose()
