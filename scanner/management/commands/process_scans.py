import logging
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from scanner.services import pipeline

logger = logging.getLogger("scanner")


class Command(BaseCommand):
    help = "Split pending scans in the inbox into photos and detect their faces."

    def add_arguments(self, parser):
        parser.add_argument(
            "--watch",
            action="store_true",
            help="Keep polling the inbox every POLL_INTERVAL_SECONDS.",
        )
        parser.add_argument(
            "--force", metavar="HASH_OR_FILE", help="Reprocess an already processed scan."
        )

    def handle(self, *args, **options):
        if options["force"]:
            scan = pipeline.find_scan(options["force"])
            if scan is None:
                raise CommandError(f"No scan matches {options['force']!r}")
            pipeline.reprocess(scan)
            return

        if not options["watch"]:
            scans = pipeline.process_inbox()
            logger.info("event=inbox_done processed=%d", len(scans))
            return

        watch_inbox()


def watch_inbox(stop=None):
    """Poll the inbox forever (or until `stop()` returns True)."""
    failed = set()
    interval = settings.POLL_INTERVAL_SECONDS
    logger.info("event=watch_started inbox=%s interval=%ss", settings.SCANS_INBOX_DIR, interval)
    while not (stop and stop()):
        close_old_connections()
        try:
            pipeline.process_inbox(skip=failed)
        except Exception:
            logger.exception("event=watch_iteration_failed")
        time.sleep(interval)
