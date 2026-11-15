"""Django settings for PhotoTag. Every deployment-specific value comes from the environment."""

import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def env(name, default=None):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name, default=""):
    return [item.strip() for item in env(name, default).split(",") if item.strip()]


DEBUG = env_bool("DJANGO_DEBUG", False)

SECRET_KEY = env("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "insecure-development-key"
    else:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY is required")

# localhost is always allowed so the container healthcheck can reach /healthz.
ALLOWED_HOSTS = [*env_list("DJANGO_ALLOWED_HOSTS"), "localhost", "127.0.0.1"]
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "axes",
    "accounts",
    "catalog",
    "scanner",
    "exporter",
    "notifications",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    # Language from the django_language cookie, else the browser's Accept-Language.
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Every view requires a session unless decorated with @login_not_required.
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
    "accounts.middleware.ForcePasswordChangeMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "axes.middleware.AxesMiddleware",
    "phototag.middleware.HtmxMiddleware",
]

ROOT_URLCONF = "phototag.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.template.context_processors.i18n",
                "catalog.context_processors.navigation",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "phototag.wsgi.application"

# Database: PostgreSQL in deployment; SQLite fallback for local development and tests.
DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=60,
        conn_health_checks=True,
    )
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Authentication
AUTH_USER_MODEL = "accounts.User"
AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "login"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 10},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Login throttling: lock out after 5 failures for 30 minutes.
AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = 0.5  # hours
AXES_RESET_ON_SUCCESS = True
AXES_LOCKOUT_PARAMETERS = ["username", "ip_address"]
AXES_LOCKOUT_TEMPLATE = "accounts/lockout.html"
# Tailscale Funnel overwrites X-Forwarded-For with the client address.
AXES_IPWARE_META_PRECEDENCE_ORDER = ["HTTP_X_FORWARDED_FOR", "REMOTE_ADDR"]

# Sessions and cookies
SESSION_COOKIE_AGE = int(env("SESSION_COOKIE_AGE", str(30 * 24 * 3600)))
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG

# Behind Tailscale Funnel: TLS terminates at the tailscale container.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
SECURE_HSTS_SECONDS = 0 if DEBUG else int(env("SECURE_HSTS_SECONDS", "3600"))
SECURE_SSL_REDIRECT = False  # Funnel only serves HTTPS; it talks plain HTTP to web internally.
SILENCED_SYSTEM_CHECKS = ["security.W008"]  # see SECURE_SSL_REDIRECT

# Internationalization
LANGUAGE_CODE = "es"
LANGUAGES = [("es", "Español"), ("en", "English")]
LOCALE_PATHS = [BASE_DIR / "locale"]
LANGUAGE_COOKIE_AGE = 365 * 24 * 3600
LANGUAGE_COOKIE_HTTPONLY = True
LANGUAGE_COOKIE_SAMESITE = "Lax"
LANGUAGE_COOKIE_SECURE = not DEBUG
TIME_ZONE = env("TZ", "Europe/Madrid")
USE_I18N = True
USE_TZ = True

# Static files
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}

# Data directories
DATA_DIR = Path(env("DATA_DIR", "/data"))
SCANS_INBOX_DIR = DATA_DIR / "scans" / "inbox"
SCANS_PROCESSED_DIR = DATA_DIR / "scans" / "processed"
PHOTOS_DIR = DATA_DIR / "photos"
THUMBS_DIR = PHOTOS_DIR / "thumbs"
EXPORT_DIR = DATA_DIR / "export"
BACKUPS_DIR = DATA_DIR / "backups"

# Scanner
POLL_INTERVAL_SECONDS = int(env("POLL_INTERVAL_SECONDS", "60"))
MIN_PHOTO_AREA_RATIO = float(env("MIN_PHOTO_AREA_RATIO", "0.03"))
CROP_INSET_PX = int(env("CROP_INSET_PX", "4"))
FACE_SCORE_THRESHOLD = float(env("FACE_SCORE_THRESHOLD", "0.7"))
YUNET_MODEL_PATH = Path(
    env("YUNET_MODEL_PATH", str(BASE_DIR / "models" / "face_detection_yunet_2023mar.onnx"))
)
THUMBNAIL_SIZES = (400, 1600)

# Network scanner (eSCL/AirScan, e.g. HP DeskJet 2700) driven from the web by the worker.
# Empty SCANNER_URL hides the feature. Example: http://192.168.1.50/eSCL
SCANNER_URL = env("SCANNER_URL", "")
SCAN_RESOLUTIONS = (300, 600)
SCAN_DEFAULT_RESOLUTION = 600
SCAN_TIMEOUT_SECONDS = int(env("SCAN_TIMEOUT_SECONDS", "300"))
SCANNER_POLL_SECONDS = int(env("SCANNER_POLL_SECONDS", "2"))
SANE_CONFIG_DIR = Path(env("SANE_CONFIG_DIR", "/tmp/phototag-sane"))

# Orientation (photos placed sideways/upside down) and near-duplicate detection.
ORIENTATION_MIN_SCORE = float(env("ORIENTATION_MIN_SCORE", "0.8"))
ORIENTATION_MARGIN = float(env("ORIENTATION_MARGIN", "1.5"))
ROTATION_POLL_SECONDS = int(env("ROTATION_POLL_SECONDS", "5"))
DUPLICATE_MAX_DISTANCE = int(env("DUPLICATE_MAX_DISTANCE", "6"))
# Sharing / downloading several photos at once.
SHARE_MAX_PHOTOS = int(env("SHARE_MAX_PHOTOS", "20"))

# Web uploads (written to the scans inbox for the worker).
UPLOAD_MAX_MB = int(env("UPLOAD_MAX_MB", "50"))
UPLOAD_MAX_FILES = int(env("UPLOAD_MAX_FILES", "20"))
DATA_UPLOAD_MAX_NUMBER_FILES = UPLOAD_MAX_FILES

# Identity suggestions (SFace). Suggestions only: identities are always voted.
SFACE_MODEL_PATH = Path(
    env("SFACE_MODEL_PATH", str(BASE_DIR / "models" / "face_recognition_sface_2021dec.onnx"))
)
FACE_MATCH_THRESHOLD = float(env("FACE_MATCH_THRESHOLD", "0.363"))
FACE_MATCH_STRONG = float(env("FACE_MATCH_STRONG", "0.5"))
EMBEDDING_POLL_SECONDS = int(env("EMBEDDING_POLL_SECONDS", "60"))

# Accounts
SUPERADMIN_USERNAME = env("SUPERADMIN_USERNAME", "admin")
SUPERADMIN_PASSWORD = env("SUPERADMIN_PASSWORD")
DEFAULT_USER_PASSWORD = env("DEFAULT_USER_PASSWORD")

# Voting defaults (can be overridden from the administration screen)
VOTE_MIN_APPROVALS = int(env("VOTE_MIN_APPROVALS", "2"))
VOTE_APPROVAL_RATIO = float(env("VOTE_APPROVAL_RATIO", "0.66"))

# Export and background jobs
EXIFTOOL_PATH = env("EXIFTOOL_PATH", "exiftool")
# Periodic automatic export every N hours (0 disables it; the admin button always works).
EXPORT_INTERVAL_HOURS = int(env("EXPORT_INTERVAL_HOURS", "0"))
EXPORT_REQUEST_POLL_SECONDS = int(env("EXPORT_REQUEST_POLL_SECONDS", "15"))

# Push notifications (Web Push / VAPID). Without keys the feature is disabled.
VAPID_PUBLIC_KEY = env("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = env("VAPID_PRIVATE_KEY", "")
VAPID_SUBJECT = env("VAPID_SUBJECT", "mailto:admin@example.com")
NOTIFY_POLL_MINUTES = int(env("NOTIFY_POLL_MINUTES", "10"))
NOTIFY_MIN_INTERVAL_MINUTES = int(env("NOTIFY_MIN_INTERVAL_MINUTES", "60"))
NOTIFY_QUIET_HOURS = env("NOTIFY_QUIET_HOURS", "22-9")

# Backups
BACKUP_RETENTION_DAYS = int(env("BACKUP_RETENTION_DAYS", "14"))
BACKUP_HOUR = int(env("BACKUP_HOUR", "3"))

# Logging: one structured key=value line per event on stdout.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "structured": {
            "format": "ts=%(asctime)s level=%(levelname)s logger=%(name)s msg=%(message)s",
        },
    },
    "handlers": {
        "stdout": {"class": "logging.StreamHandler", "formatter": "structured"},
    },
    "root": {"handlers": ["stdout"], "level": env("LOG_LEVEL", "INFO")},
    "loggers": {
        "django": {"handlers": ["stdout"], "level": "INFO", "propagate": False},
        "apscheduler": {"handlers": ["stdout"], "level": "WARNING", "propagate": False},
    },
}
