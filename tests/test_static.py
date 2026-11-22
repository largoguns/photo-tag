"""Static files build as in production (Dockerfile: collectstatic with DEBUG off).

Regressions:
- vendored Leaflet referenced its source maps (`//# sourceMappingURL=…map`), which the
  manifest storage could not find;
- Leaflet's icons (`static/vendor/leaflet/images/*.png`) were ignored by git (`*.png`), so
  they existed locally but not in the repository Portainer builds from.
Both made the image build fail only in production.
"""

import shutil
import subprocess

import pytest
from django.conf import settings
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


def test_no_static_file_is_ignored_by_git():
    """What collectstatic needs must reach the repository the server builds from."""
    base = settings.BASE_DIR
    if shutil.which("git") is None or not (base / ".git").exists():
        pytest.skip("not a git checkout")
    files = [str(path.relative_to(base)) for path in (base / "static").rglob("*") if path.is_file()]
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin"],
        cwd=base,
        input="\n".join(files),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stdout.strip() == "", f"ignored by git: {result.stdout.split()}"
