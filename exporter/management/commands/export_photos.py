from django.core.management.base import BaseCommand

from exporter.services.export import export_pending


class Command(BaseCommand):
    help = "Export cataloged photos changed since their last export (JPG + XMP sidecar)."

    def handle(self, *args, **options):
        exported, failed = export_pending()
        self.stdout.write(f"exported={exported} failed={failed}")
