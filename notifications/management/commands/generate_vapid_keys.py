from django.core.management.base import BaseCommand

from notifications.services.push import generate_vapid_keys


class Command(BaseCommand):
    help = "Print a new VAPID key pair for Web Push (set them as environment variables)."

    def handle(self, *args, **options):
        public, private = generate_vapid_keys()
        self.stdout.write(f"VAPID_PUBLIC_KEY={public}")
        self.stdout.write(f"VAPID_PRIVATE_KEY={private}")
