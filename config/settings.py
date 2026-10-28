import os
import sys
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_local_environment():
    """Load simple KEY=VALUE entries for local runserver; deployed env always wins."""
    if "pytest" in Path(sys.argv[0]).name:
        return
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for raw_line in env_file.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_local_environment()
SECRET_KEY = os.getenv("SECRET_KEY", "unsafe-local-development-key")
DEBUG = os.getenv("DEBUG", "1") == "1"
ALLOWED_HOSTS = [x.strip() for x in os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")]
if not DEBUG and SECRET_KEY == "unsafe-local-development-key":
    raise ImproperlyConfigured("SECRET_KEY must be explicitly configured in production.")

INSTALLED_APPS = [
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles",
    "apps.shops", "apps.catalog", "apps.orders", "apps.integrations",
]
MIDDLEWARE = [
    "config.request_id.RequestIDMiddleware",
    "config.views.HealthCheckMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.shops.middleware.StoreResolverMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request", "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages", "apps.shops.context.store_context",
    ]},
}]
WSGI_APPLICATION = "config.wsgi.application"
DATABASES = {"default": dj_database_url.config(
    default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}", conn_max_age=60,
    conn_health_checks=True,
)}
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "id"
TIME_ZONE = "Asia/Jakarta"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "/account/login/"
LOGIN_REDIRECT_URL = "/account/orders/"
STORAGES = {"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": (
    "django.contrib.staticfiles.storage.StaticFilesStorage" if DEBUG
    else "whitenoise.storage.CompressedManifestStaticFilesStorage"
)}}
USE_S3 = os.getenv("USE_S3", "0") == "1"
if USE_S3:
    STORAGES["default"] = {"BACKEND": "storages.backends.s3.S3Storage"}
    AWS_STORAGE_BUCKET_NAME = os.environ["AWS_STORAGE_BUCKET_NAME"]
    AWS_S3_REGION_NAME = os.getenv("AWS_S3_REGION_NAME", "ap-southeast-1")
    AWS_S3_CUSTOM_DOMAIN = os.getenv("AWS_S3_CUSTOM_DOMAIN", "") or None
    AWS_DEFAULT_ACL = None
    AWS_QUERYSTRING_AUTH = False
    AWS_S3_FILE_OVERWRITE = False
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_SSL_REDIRECT = os.getenv("SECURE_SSL_REDIRECT", "0" if DEBUG else "1") == "1"
SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "0" if DEBUG else "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = os.getenv("SECURE_HSTS_INCLUDE_SUBDOMAINS", "0") == "1"
SECURE_CONTENT_TYPE_NOSNIFF = True
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "orders@example.com")
EMAIL_BACKEND = os.getenv("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = os.getenv("EMAIL_HOST", "localhost")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "25"))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "0") == "1"
EMAIL_USE_SSL = os.getenv("EMAIL_USE_SSL", "0") == "1"
SERVER_EMAIL = os.getenv("SERVER_EMAIL", DEFAULT_FROM_EMAIL)
# "Name:email,Name:email" — receives an email with the traceback for every unhandled server
# error when DEBUG=0. Leave empty when a log-based alert (e.g. CloudWatch) covers this.
ADMINS = [tuple(entry.split(":", 1)) for entry in os.getenv("ADMINS", "").split(",") if ":" in entry]

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "request_id": {"()": "config.request_id.RequestIDFilter"},
        "require_debug_false": {"()": "django.utils.log.RequireDebugFalse"},
    },
    "formatters": {
        "standard": {"format": "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "standard",
            "filters": ["request_id"]},
        "mail_admins": {"class": "django.utils.log.AdminEmailHandler", "level": "ERROR",
            "filters": ["require_debug_false"]},
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django.request": {"handlers": ["console", "mail_admins"], "level": "WARNING",
            "propagate": False},
        "niagakit": {"handlers": ["console", "mail_admins"], "level": LOG_LEVEL,
            "propagate": False},
    },
}

MOBILE_TOKEN_TTL_DAYS = int(os.getenv("MOBILE_TOKEN_TTL_DAYS", "30"))

PAYMENT_PROVIDER = os.getenv("PAYMENT_PROVIDER", "doku" if os.getenv("DOKU_CLIENT_ID") else "demo")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
DOKU_CLIENT_ID = os.getenv("DOKU_CLIENT_ID", "")
DOKU_API_KEY = os.getenv("DOKU_API_KEY", "")
DOKU_SECRET_KEY = os.getenv("DOKU_SECRET_KEY", "")
DOKU_API_URL = os.getenv("DOKU_API_URL", "https://api-sandbox.doku.com").rstrip("/")
DOKU_PAYMENT_DUE_MINUTES = int(os.getenv("DOKU_PAYMENT_DUE_MINUTES", "60"))
XENDIT_SECRET_KEY = os.getenv("XENDIT_SECRET_KEY", "")
XENDIT_WEBHOOK_VERIFICATION_TOKEN = os.getenv("XENDIT_WEBHOOK_VERIFICATION_TOKEN", "")
XENDIT_API_URL = os.getenv("XENDIT_API_URL", "https://api.xendit.co").rstrip("/")
BITESHIP_API_KEY = os.getenv("BITESHIP_API_KEY", "")
BITESHIP_API_URL = os.getenv("BITESHIP_API_URL", "https://api.biteship.com").rstrip("/")
BITESHIP_ORIGIN_POSTAL_CODE = os.getenv("BITESHIP_ORIGIN_POSTAL_CODE", "")
BITESHIP_COURIERS = os.getenv("BITESHIP_COURIERS", "jne,jnt,sicepat,anteraja")
BITESHIP_ALLOW_LIVE = os.getenv("BITESHIP_ALLOW_LIVE", "0") == "1"
# Pickup contact for courier bookings; falls back to the store name/support phone and the
# highest-priority warehouse address when empty.
BITESHIP_ORIGIN_CONTACT_NAME = os.getenv("BITESHIP_ORIGIN_CONTACT_NAME", "")
BITESHIP_ORIGIN_CONTACT_PHONE = os.getenv("BITESHIP_ORIGIN_CONTACT_PHONE", "")
BITESHIP_ORIGIN_ADDRESS = os.getenv("BITESHIP_ORIGIN_ADDRESS", "")
SHIPPING_RATE_PROVIDER = os.getenv("SHIPPING_RATE_PROVIDER", "biteship")
RAJAONGKIR_API_KEY = os.getenv("RAJAONGKIR_API_KEY", "")
RAJAONGKIR_API_URL = os.getenv("RAJAONGKIR_API_URL", "https://rajaongkir.komerce.id/api/v1").rstrip("/")
RAJAONGKIR_ORIGIN_ID = os.getenv("RAJAONGKIR_ORIGIN_ID", "")
RAJAONGKIR_ORIGIN_POSTAL_CODE = os.getenv("RAJAONGKIR_ORIGIN_POSTAL_CODE", "")
RAJAONGKIR_COURIERS = os.getenv("RAJAONGKIR_COURIERS", "jne:sicepat:jnt:ninja:tiki:anteraja:pos")
