"""Entry point of the `worker` service: scheduler for background jobs + inbox watcher."""

import logging
from functools import wraps

from apscheduler.schedulers.background import BackgroundScheduler
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections

from exporter.services import backup, export
from scanner.management.commands.process_scans import watch_inbox
from scanner.services import embeddings

logger = logging.getLogger("worker")


def job(func):
    """Run a scheduled job with fresh DB connections and logged failures."""

    @wraps(func)
    def wrapper():
        close_old_connections()
        try:
            func()
        except Exception:
            logger.exception("event=job_failed job=%s", func.__name__)
        finally:
            close_old_connections()

    return wrapper


def build_scheduler():
    scheduler = BackgroundScheduler(timezone=settings.TIME_ZONE)
    common = {"max_instances": 1, "coalesce": True}
    scheduler.add_job(
        job(export.run_pending_requests),
        "interval",
        seconds=settings.EXPORT_REQUEST_POLL_SECONDS,
        id="export_requests",
        **common,
    )
    if settings.EXPORT_INTERVAL_HOURS:
        scheduler.add_job(
            job(export.export_pending),
            "interval",
            hours=settings.EXPORT_INTERVAL_HOURS,
            id="periodic_export",
            **common,
        )
    scheduler.add_job(
        job(embeddings.compute_missing),
        "interval",
        seconds=settings.EMBEDDING_POLL_SECONDS,
        id="face_embeddings",
        **common,
    )
    scheduler.add_job(
        job(backup.backup_database),
        "cron",
        hour=settings.BACKUP_HOUR,
        minute=0,
        id="daily_backup",
        **common,
    )
    return scheduler


class Command(BaseCommand):
    help = "Run the background worker: scheduled jobs plus `process_scans --watch`."

    def handle(self, *args, **options):
        scheduler = build_scheduler()
        scheduler.start()
        logger.info("event=worker_started jobs=%s", [j.id for j in scheduler.get_jobs()])
        try:
            watch_inbox()
        finally:
            scheduler.shutdown(wait=False)
