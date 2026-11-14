import logging

from django.core.management.base import BaseCommand

from accounts.services.users import ensure_superadmin

logger = logging.getLogger("accounts")


class Command(BaseCommand):
    help = "Create the initial superadmin from SUPERADMIN_USERNAME/SUPERADMIN_PASSWORD if needed."

    def handle(self, *args, **options):
        user = ensure_superadmin()
        if user:
            logger.info("event=superadmin_created username=%s", user.username)
        else:
            logger.info("event=superadmin_exists")
