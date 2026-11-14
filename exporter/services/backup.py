"""Daily PostgreSQL dump with retention (spec section 8.3)."""

import logging
import os
import subprocess
import time
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.utils import timezone

logger = logging.getLogger("backup")

BACKUP_PREFIX = "phototag-"
BACKUP_SUFFIX = ".dump"


def _pg_env():
    db = settings.DATABASES["default"]
    env = os.environ.copy()
    env.update(
        {
            "PGHOST": str(db.get("HOST") or "localhost"),
            "PGPORT": str(db.get("PORT") or 5432),
            "PGUSER": str(db.get("USER") or ""),
            "PGPASSWORD": str(db.get("PASSWORD") or ""),
            "PGDATABASE": str(db.get("NAME") or ""),
        }
    )
    return env


def prune_backups(directory=None, retention_days=None, now=None):
    """Delete dumps older than the retention period. Returns the deleted paths."""
    directory = Path(directory or settings.BACKUPS_DIR)
    retention_days = retention_days or settings.BACKUP_RETENTION_DAYS
    cutoff = (now or time.time()) - retention_days * 86400
    deleted = []
    for path in directory.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}"):
        if path.stat().st_mtime < cutoff:
            path.unlink()
            deleted.append(path)
    return deleted


def backup_database():
    if connection.vendor != "postgresql":
        logger.warning("event=backup_skipped reason=not_postgresql")
        return None
    directory = Path(settings.BACKUPS_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M%S")
    target = directory / f"{BACKUP_PREFIX}{stamp}{BACKUP_SUFFIX}"
    tmp = target.with_suffix(".tmp")
    subprocess.run(
        ["pg_dump", "--format=custom", "--no-owner", f"--file={tmp}"],
        env=_pg_env(),
        check=True,
        capture_output=True,
        timeout=3600,
    )
    tmp.replace(target)
    deleted = prune_backups(directory)
    logger.info(
        "event=backup_done file=%s bytes=%d pruned=%d",
        target.name,
        target.stat().st_size,
        len(deleted),
    )
    return target
