"""Static files build as in production (Dockerfile: collectstatic with DEBUG off).

Regression: vendored Leaflet referenced its source maps (`//# sourceMappingURL=…map`),
which the manifest storage could not find, so the image build failed only in production.
"""

from django.core.management import call_command
from django.test import override_settings

MANIFEST = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}


def test_collectstatic_with_the_production_storage(tmp_path):
    with override_settings(STORAGES=MANIFEST, STATIC_ROOT=tmp_path, DEBUG=False):
        call_command("collectstatic", "--noinput", verbosity=0)
    assert (tmp_path / "staticfiles.json").exists()
    assert list((tmp_path / "vendor" / "leaflet").glob("leaflet.*.js"))
