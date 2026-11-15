import os
import subprocess
import time

import cv2
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse

from catalog.models import Upload
from scanner.models import ScanJob
from scanner.services import pipeline, scanning
from tests.conftest import FOUR_PHOTOS, synthetic_scan

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(scanning.time, "sleep", lambda seconds: None)


@pytest.fixture
def scanner(settings, tmp_path):
    settings.SCANNER_URL = "http://192.168.18.46/eSCL"
    settings.SANE_CONFIG_DIR = tmp_path / "sane"
    scanning._option_support.clear()
    yield settings
    scanning._option_support.clear()


class FakeScanimage:
    """Stands in for subprocess.run(["scanimage", ...])."""

    OPEN_FAILED = b"scanimage: open of device airscan:e0:PhotoTag failed: Error during device I/O"

    def __init__(
        self, returncode=0, stderr=b"", timeout=False, writes=True, intent=True, open_failures=0
    ):
        self.returncode, self.stderr = returncode, stderr
        self.timeout, self.writes, self.intent = timeout, writes, intent
        self.open_failures = open_failures  # first N opens fail, like a sleeping printer
        self.calls = []

    def __call__(self, args, **kwargs):
        if self.open_failures:
            self.open_failures -= 1
            if "-A" not in args:
                self.calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 1, b"", self.OPEN_FAILED)
        if "-A" in args:  # option list, asked once to detect --scan-intent support
            options = b"--resolution 300|600dpi\n" + (
                b"--scan-intent Photo\n" if self.intent else b""
            )
            return subprocess.CompletedProcess(args, 0, options, b"")
        self.calls.append((args, kwargs))
        if self.timeout:
            raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))
        if self.writes and "-o" in args:
            output = args[args.index("-o") + 1]
            ok, data = cv2.imencode(".png", synthetic_scan(FOUR_PHOTOS))
            with open(output, "wb") as handle:
                handle.write(data.tobytes())
        return subprocess.CompletedProcess(args, self.returncode, b"", self.stderr)


def test_sane_config_declares_the_printer_by_url(scanner):
    directory = scanning.sane_config_dir()
    config = (directory / "airscan.conf").read_text()
    assert '"PhotoTag" = http://192.168.18.46/eSCL, eSCL' in config
    assert "discovery = disable" in config
    assert (directory / "dll.conf").read_text().strip() == "airscan"


def test_full_cycle_from_request_to_processed_scan(scanner, data_dirs, admin_user, monkeypatch):
    fake = FakeScanimage()
    monkeypatch.setattr(subprocess, "run", fake)
    job = scanning.request_scan(admin_user, 600)
    assert job.status == ScanJob.Status.QUEUED

    job = scanning.run_pending_scan()

    assert job.status == ScanJob.Status.DONE
    args, kwargs = fake.calls[0]
    assert args[:3] == ["scanimage", "-d", "airscan:e0:PhotoTag"]
    assert ["--resolution", "600"] == args[
        args.index("--resolution") : args.index("--resolution") + 2
    ]
    assert "--format=png" in args
    assert args[args.index("--scan-intent") + 1] == "Photo"
    assert kwargs["env"]["SANE_CONFIG_DIR"] == str(scanner.SANE_CONFIG_DIR)
    files = os.listdir(data_dirs / "scans" / "inbox")
    assert len(files) == 1 and files[0].startswith("scan-") and files[0].endswith(".png")
    assert job.upload.kind == Upload.Kind.SCAN and job.upload.user == admin_user

    # The regular upload flow takes over: cropping and attribution.
    path = data_dirs / "scans" / "inbox" / files[0]
    old = time.time() - 60
    os.utime(path, (old, old))
    scan = pipeline.process_inbox()[0]
    assert scan.uploaded_by == admin_user and scan.photos.count() == 4
    job.upload.refresh_from_db()
    assert job.upload.status == Upload.Status.PROCESSED


@pytest.mark.parametrize(
    ("fake", "message"),
    [
        (FakeScanimage(timeout=True), "tardado demasiado"),
        (
            FakeScanimage(
                returncode=1,
                stderr=b"scanimage: open of device failed: Invalid argument",
                writes=False,
            ),
            "No se encuentra el escáner",
        ),
        (
            FakeScanimage(returncode=1, stderr=b"scanimage: sane_start: Device busy", writes=False),
            "ocupado",
        ),
        (FakeScanimage(returncode=1, stderr=b"I/O error", writes=False), "no responde"),
        (
            FakeScanimage(
                returncode=1, stderr=b"scanimage: unrecognized option '--x'", writes=False
            ),
            "no admite una opción",
        ),
    ],
)
def test_failures_leave_a_clear_message_and_no_files(
    scanner, data_dirs, admin_user, monkeypatch, fake, message
):
    monkeypatch.setattr(subprocess, "run", fake)
    scanning.request_scan(admin_user, 300)

    job = scanning.run_pending_scan()

    assert job.status == ScanJob.Status.FAILED and message in job.error
    assert os.listdir(data_dirs / "scans" / "inbox") == []
    assert not Upload.objects.exists()


def test_only_one_scan_at_a_time(scanner, data_dirs, admin_user, monkeypatch):
    monkeypatch.setattr(subprocess, "run", FakeScanimage())
    ScanJob.objects.create(requested_by=admin_user, resolution=600, status=ScanJob.Status.SCANNING)
    scanning.request_scan(admin_user, 600)
    assert scanning.run_pending_scan() is None


def test_nothing_runs_without_scanner_url(settings, admin_user, monkeypatch):
    settings.SCANNER_URL = ""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("must not scan"))
    ScanJob.objects.create(requested_by=admin_user, resolution=600)
    assert scanning.run_pending_scan() is None


def test_unsupported_resolution_is_refused(scanner, admin_user):
    with pytest.raises(ValueError):
        scanning.request_scan(admin_user, 1200)


# --- Views ---------------------------------------------------------------------------


def test_scan_page_is_for_admins_only(client, scanner, alice, admin_user):
    assert client.get(reverse("scan")).status_code == 302
    client.force_login(alice)
    assert client.get(reverse("scan")).status_code == 403
    client.force_login(admin_user)
    content = client.get(reverse("scan")).content.decode()
    assert "¿Todo listo para escanear?" in content
    assert 'name="resolution" value="600" checked' in content


def test_scan_page_explains_setup_without_scanner_url(client, settings, admin_user):
    settings.SCANNER_URL = ""
    client.force_login(admin_user)
    content = client.get(reverse("scan")).content.decode()
    assert "SCANNER_URL" in content and "¿Todo listo para escanear?" not in content
    assert reverse("scan") not in client.get(reverse("home")).content.decode()


def test_requesting_a_scan_polls_until_done(client, scanner, data_dirs, admin_user, monkeypatch):
    client.force_login(admin_user)
    response = client.post(reverse("scan"), {"resolution": "600"}, HTTP_HX_REQUEST="true")
    content = response.content.decode()
    assert ScanJob.objects.get().status == ScanJob.Status.QUEUED
    assert 'hx-trigger="every 2s"' in content and "Preparando el escáner" in content

    # A second click while busy does not queue another scan.
    client.post(reverse("scan"), {"resolution": "600"}, HTTP_HX_REQUEST="true")
    assert ScanJob.objects.count() == 1

    monkeypatch.setattr(subprocess, "run", FakeScanimage())
    scanning.run_pending_scan()
    content = client.get(reverse("scan"), HTTP_HX_REQUEST="true").content.decode()
    assert "Escaneo enviado al procesado" in content and "Recortando las fotos" in content
    assert "¿Todo listo para escanear?" in content


def test_invalid_resolution_from_the_form(client, scanner, admin_user):
    client.force_login(admin_user)
    assert client.post(reverse("scan"), {"resolution": "9999"}).status_code == 400
    assert not ScanJob.objects.exists()


def test_failed_scan_offers_retry(client, scanner, admin_user):
    ScanJob.objects.create(
        requested_by=admin_user, resolution=600, status=ScanJob.Status.FAILED, error="Sin papel"
    )
    client.force_login(admin_user)
    content = client.get(reverse("scan")).content.decode()
    assert "Sin papel" in content and "Reintentar" in content


def test_scanner_check_command(scanner, monkeypatch, capsys):
    calls = []

    def fake(args, **kwargs):
        calls.append(args)
        out = (
            b"device `airscan:e0:PhotoTag' is a eSCL HP DeskJet 2700 series"
            if "-L" in args
            else b"--resolution 75|100|200|300|600|1200dpi"
        )
        return subprocess.CompletedProcess(args, 0, out, b"")

    monkeypatch.setattr(subprocess, "run", fake)
    call_command("scanner_check")
    output = capsys.readouterr().out
    assert "HP DeskJet 2700" in output and "Scanner reachable" in output


def test_scanner_check_without_url(settings):
    settings.SCANNER_URL = ""
    with pytest.raises(CommandError):
        call_command("scanner_check")


def test_scan_intent_is_skipped_when_sane_airscan_is_older(
    scanner, data_dirs, admin_user, monkeypatch
):
    # Regression: Ubuntu's sane-airscan rejects --scan-intent ("unrecognized option").
    fake = FakeScanimage(intent=False)
    monkeypatch.setattr(subprocess, "run", fake)
    scanning.request_scan(admin_user, 300)

    job = scanning.run_pending_scan()

    assert job.status == ScanJob.Status.DONE
    assert "--scan-intent" not in fake.calls[0][0]


def test_sleeping_printer_is_retried_until_it_answers(scanner, data_dirs, admin_user, monkeypatch):
    # Regression: the HP DeskJet refuses the first connections while it wakes up.
    fake = FakeScanimage(open_failures=4)  # option query + scan fail twice, then it wakes up
    monkeypatch.setattr(subprocess, "run", fake)
    scanning.request_scan(admin_user, 600)

    job = scanning.run_pending_scan()

    assert job.status == ScanJob.Status.DONE
    assert len(fake.calls) == 3
    assert "--scan-intent" in fake.calls[-1][0]  # asked again once the printer answered
    assert len(os.listdir(data_dirs / "scans" / "inbox")) == 1


def test_printer_that_never_answers_fails_after_the_retries(
    scanner, data_dirs, admin_user, monkeypatch
):
    fake = FakeScanimage(open_failures=99)
    monkeypatch.setattr(subprocess, "run", fake)
    scanning.request_scan(admin_user, 600)

    job = scanning.run_pending_scan()

    assert job.status == ScanJob.Status.FAILED and "no responde" in job.error
    assert len(fake.calls) == len(scanning.OPEN_RETRY_DELAYS) + 1
    assert os.listdir(data_dirs / "scans" / "inbox") == []


def test_failed_option_query_is_not_remembered(scanner, monkeypatch):
    monkeypatch.setattr(subprocess, "run", FakeScanimage(open_failures=1))
    assert scanning.photo_intent_args() == []
    assert scanning.photo_intent_args() == ["--scan-intent", "Photo"]
