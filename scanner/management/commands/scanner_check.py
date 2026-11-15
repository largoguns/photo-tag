from django.core.management.base import BaseCommand, CommandError

from scanner.services import scanning


class Command(BaseCommand):
    help = "Check the connection with the network scanner configured in SCANNER_URL."

    def handle(self, *args, **options):
        if not scanning.is_configured():
            raise CommandError("SCANNER_URL is not set.")
        try:
            listing, details = scanning.check()
        except FileNotFoundError as exc:
            raise CommandError("scanimage is not installed (run this in the worker).") from exc
        self.stdout.write("$ scanimage -L")
        self.stdout.write(
            listing.stdout.decode(errors="replace") or listing.stderr.decode(errors="replace")
        )
        self.stdout.write(f"$ scanimage -d {scanning.DEVICE} -A")
        self.stdout.write(
            details.stdout.decode(errors="replace") or details.stderr.decode(errors="replace")
        )
        if details.returncode != 0:
            raise CommandError("The scanner did not answer. Check SCANNER_URL and the printer.")
        self.stdout.write(self.style.SUCCESS("Scanner reachable."))
