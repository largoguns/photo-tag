"""Compute missing perceptual hashes and flag near-duplicates among processed photos."""

import cv2
from django.core.management.base import BaseCommand

from catalog.models import Photo
from scanner.services import dedupe, images
from scanner.services.pipeline import _flag_duplicates


class Command(BaseCommand):
    help = (
        "Find possible duplicate photos (re-scans, recompressed copies) and flag them for review."
    )

    def handle(self, *args, **options):
        hashed = 0
        variants = {}
        for photo in Photo.objects.visible().order_by("pk"):
            image = cv2.imread(str(images.photo_path(photo.file_name)))
            if image is None:
                continue
            if not photo.phash:
                photo.phash = dedupe.dhash(image)
                Photo.objects.filter(pk=photo.pk).update(phash=photo.phash)
                hashed += 1
            variants[photo.pk] = dedupe.hash_variants(image)

        known = [(pk, Photo.objects.get(pk=pk).phash) for pk in variants]
        flagged = 0
        for pk, photo_variants in variants.items():
            photo = Photo.objects.select_related("scan").get(pk=pk)
            if photo.duplicate_of_id:
                continue
            # Only compare against earlier photos: the oldest copy is the original.
            earlier = [(other, h) for other, h in known if other < pk and h]
            original = dedupe.find_duplicate(photo_variants, exclude_pk=pk, candidates=earlier)
            if original:
                Photo.objects.filter(pk=pk).update(duplicate_of=original)
                _flag_duplicates(photo.scan, [original])
                flagged += 1
        self.stdout.write(f"hashed={hashed} flagged={flagged}")
