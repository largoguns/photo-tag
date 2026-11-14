from django.core.management.base import BaseCommand

from catalog.models import Face
from scanner.services.embeddings import compute_missing


class Command(BaseCommand):
    help = "Compute missing face embeddings used for identity suggestions."

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true", help="Recompute every embedding.")

    def handle(self, *args, **options):
        if options["all"]:
            Face.objects.update(embedding=None)
        total = 0
        while done := compute_missing():
            total += done
        self.stdout.write(f"computed={total}")
