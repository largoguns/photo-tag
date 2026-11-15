from django.core.management.base import BaseCommand

from gamification.services import scoring


class Command(BaseCommand):
    help = "Recompute every point and badge from the existing history (no notifications)."

    def handle(self, *args, **options):
        count = scoring.rebuild()
        self.stdout.write(self.style.SUCCESS(f"{count} score events rebuilt."))
