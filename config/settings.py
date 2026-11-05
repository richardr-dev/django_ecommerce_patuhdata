import os
import sys
from decimal import Decimal
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_local_environment():
    """Load simple KEY=VALUE entries for local runserver; deployed env always wins.

    Tests take only DATABASE_URL, so they run on the local PostgreSQL while provider
    credentials in .env never reach the test suite."""
    # sys.modules, not argv: "python -m pytest" runs with argv[0] ending in __main__.py.
    only = {"DATABASE_URL"} if "pytest" in sys.modules else None
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for raw_line in env_file.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if only is None or key.strip() in only:
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_local_environment()
SECRET_KEY = os.getenv("SECRET_KEY", "unsafe-local-development-key")
DEBUG = os.getenv("DEBUG", "1") == "1"
ALLOWED_HOSTS = [x.strip() for x in os.getenv("ALLOWED_HOSTS",
    "localhost,127.0.0.1,.localhost,testserver").split(",")]
if not DEBUG and SECRET_KEY == "unsafe-local-development-key":
    raise ImproperlyConfigured("SECRET_KEY must be explicitly configured in production.")

INSTALLED_APPS = [
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles",
    "apps.shops", "apps.catalog", "apps.orders", "apps.integrations", "apps.billing",
    "apps.accounting", "apps.channels", "apps.tax",
]
MIDDLEWARE = [
    "config.request_id.RequestIDMiddleware",
    "config.views.HealthCheckMiddleware",
    # Caddy's on-demand TLS check (http://web:8000/internal/tls-ask/), before host validation.
    "apps.shops.views.TLSAskMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.shops.middleware.StoreResolverMiddleware",
    "apps.shops.middleware.StorefrontGateMiddleware",
    "apps.shops.middleware.SellerShortcutMiddleware",
    "apps.shops.middleware.PrivatePagesMiddleware",
    "apps.shops.middleware.ReferralCaptureMiddleware",
    "apps.shops.middleware.CampaignAttributionMiddleware",
]
# Sellers sign in with their email or WhatsApp number (apps/shops/auth.py).
AUTHENTICATION_BACKENDS = ["django.contrib.auth.backends.ModelBackend",
    "apps.shops.auth.EmailOrPhoneBackend"]
ROOT_URLCONF = "config.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request", "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages", "apps.shops.context.store_context",
        "apps.shops.context.platform_context",
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
# Uploaded images in the platform's one S3 bucket, under media/ (public read); the same bucket
# keeps private database backups under backups/. Credentials come from the server's instance
# role, so no access keys are stored. See deploy/aws/ec2-t3-small.yaml.
USE_S3 = os.getenv("USE_S3", "0") == "1"
if USE_S3:
    STORAGES["default"] = {"BACKEND": "storages.backends.s3.S3Storage"}
    AWS_STORAGE_BUCKET_NAME = os.environ["AWS_STORAGE_BUCKET_NAME"]
    AWS_S3_REGION_NAME = os.getenv("AWS_S3_REGION_NAME", "ap-southeast-3")
    # Newer regions such as Jakarta only answer on their regional endpoint.
    AWS_S3_CUSTOM_DOMAIN = os.getenv("AWS_S3_CUSTOM_DOMAIN", "") or (
        f"{AWS_STORAGE_BUCKET_NAME}.s3.{AWS_S3_REGION_NAME}.amazonaws.com")
    AWS_S3_SIGNATURE_VERSION = "s3v4"
    AWS_S3_ADDRESSING_STYLE = "virtual"
    AWS_LOCATION = "media"
    AWS_DEFAULT_ACL = None
    AWS_QUERYSTRING_AUTH = False
    AWS_S3_FILE_OVERWRITE = False
    AWS_S3_OBJECT_PARAMETERS = {"CacheControl": "public, max-age=604800"}
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = not DEBUG
# Sign-ins last SESSION_DAYS (default 7) and the cookie is never readable by page scripts.
SESSION_COOKIE_AGE = int(os.getenv("SESSION_DAYS", "7")) * 24 * 3600
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
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
# SMTP2GO over its HTTP API (EMAIL_BACKEND=apps.integrations.email.Smtp2goBackend), using an API
# key instead of an SMTP user.
# Shoppers see a store only once it can take orders, and a lapsed store as temporarily closed
# (apps/shops/availability.py). Off only in the test suite, whose stores are bare.
STOREFRONT_GATE = os.getenv("STOREFRONT_GATE", "1") == "1"
SMTP2GO_API_KEY = os.getenv("SMTP2GO_API_KEY", "")
SMTP2GO_API_URL = os.getenv("SMTP2GO_API_URL", "https://api.smtp2go.com/v3").rstrip("/")
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

# "db" shares cache entries (rate-limit counters) across every worker and container; the
# per-process memory cache suits tests and single-process runserver only.
CACHE_BACKEND = os.getenv("CACHE_BACKEND", "locmem")
CACHES = {"default": {"BACKEND": "django.core.cache.backends.db.DatabaseCache",
    "LOCATION": "niagakit_cache"} if CACHE_BACKEND == "db" else
    {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "1") == "1"
# Reverse proxies in front of Django that append X-Forwarded-For (Caddy in compose.prod.yaml = 1).
TRUSTED_PROXY_HOPS = int(os.getenv("TRUSTED_PROXY_HOPS", "0"))

# Hosted error tracking. Empty disables it; no request bodies or personal data are sent.
SENTRY_DSN = os.getenv("SENTRY_DSN", "")
if SENTRY_DSN:
    import sentry_sdk

    sentry_sdk.init(dsn=SENTRY_DSN, environment=os.getenv("SENTRY_ENVIRONMENT", "production"),
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0")),
        send_default_pii=False, max_request_body_size="never")

# Self-serve platform: sellers sign up at PLATFORM_DOMAIN and get <slug>.PLATFORM_DOMAIN.
# Empty keeps single-host mode (store chosen by custom domain or ?store=slug).
PLATFORM_DOMAIN = os.getenv("PLATFORM_DOMAIN", "").strip().lower()
# The store-address suffix shown to sellers; lets a local copy show "lakuin.store" while its
# real links stay on PLATFORM_DOMAIN (localhost).
PLATFORM_DISPLAY_DOMAIN = os.getenv("PLATFORM_DISPLAY_DOMAIN", "").strip().lower()
PLATFORM_NAME = os.getenv("PLATFORM_NAME", "Lakuin")
PLATFORM_TAGLINE = os.getenv("PLATFORM_TAGLINE", "Biar Jualan Makin Laku")
# Scheme and port for links to the platform and store subdomains. Defaults to PUBLIC_BASE_URL;
# set it when PUBLIC_BASE_URL is a tunnel for webhooks (e.g. ngrok) but people browse localhost.
PLATFORM_BASE_URL = os.getenv("PLATFORM_BASE_URL", "").rstrip("/")
# The company operating the platform: copyright, legal pages and the platform footer.
PLATFORM_COMPANY_NAME = os.getenv("PLATFORM_COMPANY_NAME", "PT Patuhdata Solusi Nusantara")
PLATFORM_COMPANY_URL = os.getenv("PLATFORM_COMPANY_URL", "https://patuhdata.id")
PLATFORM_SUPPORT_EMAIL = os.getenv("PLATFORM_SUPPORT_EMAIL", "")
# Where the Custom plan's "Hubungi sales" button points (e.g. a wa.me link); falls back to
# emailing PLATFORM_SUPPORT_EMAIL.
PLATFORM_SALES_URL = os.getenv("PLATFORM_SALES_URL", "")
# Days of the default plan a new store gets free at signup when that plan is paid.
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "14"))
# Ajak teman: each side of a referral gets this percent off one month of its plan (1-90).
REFERRAL_DISCOUNT_PERCENT = int(os.getenv("REFERRAL_DISCOUNT_PERCENT", "50"))
# Ajak teman tiers for the inviting store: (paid invites needed, name, % off one month each).
REFERRAL_TIERS = [(1, "Teman", 50), (3, "Mitra", 70), (10, "Duta Lakuin", 90)]
# Renewal billing: bill the next period this many days ahead, and keep a paying store fully
# active this many days after its period ends while the renewal is unpaid.
RENEWAL_NOTICE_DAYS = int(os.getenv("RENEWAL_NOTICE_DAYS", "7"))
RENEWAL_GRACE_DAYS = int(os.getenv("RENEWAL_GRACE_DAYS", "3"))

MOBILE_TOKEN_TTL_DAYS = int(os.getenv("MOBILE_TOKEN_TTL_DAYS", "30"))

# Buyers pay each seller's own DOKU/Xendit account or bank account (back office > Pembayaran).
# PAYMENT_PROVIDER is the platform's own account: Lakuin subscriptions and the local demo. Store
# checkouts use it only when PLATFORM_COLLECTS_PAYMENTS=1, because collecting buyers' money for
# sellers needs a payment licence (or a split-payment partner agreement).
PAYMENT_PROVIDER = os.getenv("PAYMENT_PROVIDER", "doku" if os.getenv("DOKU_CLIENT_ID") else "demo")
PLATFORM_COLLECTS_PAYMENTS = os.getenv("PLATFORM_COLLECTS_PAYMENTS", "0") == "1"
# "DOKU Lakuin": a seller may opt in (Back office > Pembayaran) to take QRIS / VA / e-wallet through
# the platform's DOKU account; Lakuin then pays out their balance. PLATFORM_DOKU_FEE_PERCENT is
# Lakuin's fee on those orders, deducted from the balance.
PLATFORM_DOKU_FOR_STORES = os.getenv("PLATFORM_DOKU_FOR_STORES", "1") == "1"
PLATFORM_DOKU_FEE_PERCENT = Decimal(os.getenv("PLATFORM_DOKU_FEE_PERCENT", "0"))
# Encrypts sellers' gateway secrets; defaults to SECRET_KEY, so set it before rotating SECRET_KEY.
PAYMENT_SECRETS_KEY = os.getenv("PAYMENT_SECRETS_KEY", "")
BANK_TRANSFER_DUE_HOURS = int(os.getenv("BANK_TRANSFER_DUE_HOURS", "24"))
# The demo provider marks orders paid without money changing hands, so it only works in
# development unless a production trial opts in explicitly.
ALLOW_DEMO_PAYMENTS = os.getenv("ALLOW_DEMO_PAYMENTS", "1" if DEBUG else "0") == "1"
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
