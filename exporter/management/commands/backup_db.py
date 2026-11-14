from django.core.management.base import BaseCommand

from exporter.services.backup import backup_database


class Command(BaseCommand):
    help = "Dump the PostgreSQL database to BACKUPS_DIR and prune old dumps."

    def handle(self, *args, **options):
        path = backup_database()
        self.stdout.write(str(path) if path else "skipped")
