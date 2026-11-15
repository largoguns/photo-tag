"""Immediate push notification when a scan requested from the scanner screen finishes."""

import os
import subprocess
import time
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse

from catalog.models import Upload
from notifications.models import NotificationSettings, PushSubscription
from notifications.services import push
from scanner.models import ScanJob
from scanner.services import pipeline, scanning
from tests.scanner.test_scanning import FakeScanimage

pytestmark = pytest.mark.django_db


@pytest.fixture
def ready(settings, tmp_path, data_dirs, monkeypatch, admin_user):
    settings.SCANNER_URL = "http://192.168.18.46/eSCL"
    settings.SANE_CONFIG_DIR = tmp_path / "sane"
    settings.VAPID_PUBLIC_KEY, settings.VAPID_PRIVATE_KEY = push.generate_vapid_keys()
    settings.NOTIFY_QUIET_HOURS = "0-23"  # quiet all day: scan notifications ignore it
    scanning._option_support.clear()
    PushSubscription.objects.create(
        user=admin_user, endpoint="https://push.example.com/admin", p256dh="k", auth="a"
    )
    sent = []
    monkeypatch.setattr(push, "send", lambda sub, payload: sent.append(payload) or True)
    return sent


def scan_and_process(monkeypatch, admin_user, fake=None):
    """Scan with a fake scanimage, then let the pipeline process the inbox."""
    monkeypatch.setattr(subprocess, "run", fake or FakeScanimage())
    scanning.request_scan(admin_user, 600)
    job = scanning.run_pending_scan()
    old = time.time() - 60  # settled: old enough for the inbox watcher
    for path in Path(settings.SCANS_INBOX_DIR).iterdir():
        os.utime(path, (old, old))
    pipeline.process_inbox()
    return job


def test_processed_scan_notifies_the_requester(ready, monkeypatch, admin_user):
    job = scan_and_process(monkeypatch, admin_user)

    (payload,) = ready
    assert payload["tag"] == "phototag-scan"
    assert payload["body"].startswith("4 fotos listas del escaneo de las")
    job.upload.refresh_from_db()  # the pipeline linked the scan after the job finished
    first = job.upload.scan.photos.order_by("index").first()
    assert payload["url"] == reverse("photo_detail", args=[first.pk])


def test_failed_scan_notifies_with_the_reason(ready, monkeypatch, admin_user):
    fake = FakeScanimage(returncode=1, stderr=b"I/O error", writes=False)
    job = scan_and_process(monkeypatch, admin_user, fake)

    assert job.status == ScanJob.Status.FAILED
    (payload,) = ready
    assert payload["body"].startswith("No se ha podido escanear: La impresora no responde")
    assert payload["url"] == reverse("scan")


def test_duplicate_scan_is_reported(ready, monkeypatch, admin_user):
    scan_and_process(monkeypatch, admin_user)
    ready.clear()
    scan_and_process(monkeypatch, admin_user)  # same synthetic page again

    (payload,) = ready
    assert "ya estaba en PhotoTag" in payload["body"]
    assert Upload.objects.filter(status=Upload.Status.DUPLICATE).count() == 1


def test_preference_turns_scan_notifications_off(ready, monkeypatch, admin_user):
    prefs = NotificationSettings.for_user(admin_user)
    prefs.scans = False
    prefs.save()
    scan_and_process(monkeypatch, admin_user)
    assert ready == []


def test_regular_uploads_do_not_notify(ready, alice, data_dirs, settings):
    from scanner.services.uploads import save_upload
    from tests.scanner.test_uploads import image_file, settle_inbox

    save_upload(alice, image_file(), Upload.Kind.SINGLE)
    settle_inbox(settings)
    pipeline.process_inbox()
    assert ready == []


def test_service_worker_skips_scan_notification_on_the_scan_screen(client):
    body = client.get("/sw.js").content.decode()
    assert "scanScreenVisible" in body and '"/admin/scan/"' in body


def test_profile_lists_the_scan_preference(client, admin_user, settings):
    settings.VAPID_PUBLIC_KEY, settings.VAPID_PRIVATE_KEY = push.generate_vapid_keys()
    client.force_login(admin_user)
    assert "Mis escaneos terminados" in client.get(reverse("profile")).content.decode()
