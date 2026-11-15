"""Perceptual hashes to spot re-scans and recompressed copies of the same photo.

The SHA-256 check only catches byte-identical files; a difference hash (dHash) survives
recompression, resizing and small exposure changes. Hashes are compared in the four
orientations, so a re-scan placed differently on the glass is also found.
"""

import cv2
import numpy as np
from django.conf import settings

from catalog.models import Photo
from scanner.services.orientation import ROTATIONS, rotate

# Flat or nearly empty images give meaningless hashes (every blank crop would match).
MIN_DETAIL = 6.0


def dhash(image):
    """64-bit difference hash as 16 hex characters, or "" for featureless images."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    small = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA).astype(np.int16)
    if float(small.std()) < MIN_DETAIL:
        return ""
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return f"{value:016x}"


def hash_variants(image):
    """Hashes of the image in the four orientations (empty ones dropped)."""
    return [h for h in (dhash(rotate(image, degrees)) for degrees in ROTATIONS) if h]


def hamming(a, b):
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def find_duplicate(variants, exclude_pk=None, candidates=None):
    """Earliest visible photo whose hash is close to any of `variants`, or None."""
    if not variants:
        return None
    if candidates is None:
        candidates = (
            Photo.objects.visible()
            .exclude(phash="")
            .exclude(pk=exclude_pk)
            .order_by("pk")
            .values_list("pk", "phash")
        )
    limit = settings.DUPLICATE_MAX_DISTANCE
    for pk, phash in candidates:
        if pk != exclude_pk and any(hamming(variant, phash) <= limit for variant in variants):
            return pk
    return None
