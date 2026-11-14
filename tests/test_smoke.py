import pytest
from django.conf import settings
from django.core.management import call_command

from scanner.management.commands.run_worker import build_scheduler


def test_security_settings():
    assert settings.SECURE_PROXY_SSL_HEADER == ("HTTP_X_FORWARDED_PROTO", "https")
    assert settings.SESSION_COOKIE_HTTPONLY
    assert settings.SESSION_COOKIE_SECURE
    assert settings.CSRF_COOKIE_SECURE
    assert not settings.DEBUG
    assert settings.SESSION_COOKIE_AGE == 30 * 24 * 3600
    assert settings.AXES_FAILURE_LIMIT == 5
    assert settings.AXES_COOLOFF_TIME == 0.5
    min_length = next(
        v["OPTIONS"]["min_length"]
        for v in settings.AUTH_PASSWORD_VALIDATORS
        if v["NAME"].endswith("MinimumLengthValidator")
    )
    assert min_length == 10
    assert "django.contrib.auth.middleware.LoginRequiredMiddleware" in settings.MIDDLEWARE


def test_no_django_admin_or_public_media():
    assert "django.contrib.admin" not in settings.INSTALLED_APPS
    assert not settings.MEDIA_ROOT


@pytest.mark.django_db
def test_migrations_are_complete():
    call_command("makemigrations", "--check", "--dry-run", verbosity=0)


def test_worker_schedules_jobs():
    scheduler = build_scheduler()
    assert {job.id for job in scheduler.get_jobs()} >= {"export_requests", "daily_backup"}
