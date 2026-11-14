"""Settings for the test suite: self-contained, no secrets needed from the environment."""

import os
import tempfile

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-secret-key")
os.environ.setdefault("DEFAULT_USER_PASSWORD", "Generica-2024!")
os.environ.setdefault("SUPERADMIN_PASSWORD", "Superadmin-2024!")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="phototag-test-"))

from phototag.settings import *  # noqa: E402,F403
from phototag.settings import STORAGES  # noqa: E402

STATIC_ROOT = tempfile.mkdtemp(prefix="phototag-static-")
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
STORAGES = {
    **STORAGES,
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
