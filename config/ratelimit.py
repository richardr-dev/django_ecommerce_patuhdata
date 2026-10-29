"""Fixed-window request limits for abuse-prone endpoints (logins, sign-ups, forms).

Counters live in the default cache. Production uses the database cache so every gunicorn worker
and container shares one count; the per-process memory cache would let each worker grant its own
allowance.
"""
import functools
import hashlib
import time

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.template.loader import render_to_string


def client_ip(request):
    """The client address, trusting X-Forwarded-For only for the configured proxy hops.

    Behind Caddy (one hop) REMOTE_ADDR is the proxy, so the real client is the rightmost
    address the proxy appended. Anything further left is client-supplied and spoofable.
    """
    hops = settings.TRUSTED_PROXY_HOPS
    forwarded = [part.strip() for part in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")
        if part.strip()]
    if hops and len(forwarded) >= hops:
        return forwarded[-hops]
    return request.META.get("REMOTE_ADDR", "")


def _hit(key, window):
    """Count one request in the current window and return (count, seconds until reset)."""
    now = int(time.time())
    window_start = now - now % window
    bucket = f"rl:{key}:{window_start}"
    if cache.add(bucket, 1, timeout=window + 5):
        count = 1
    else:
        try:
            count = cache.incr(bucket)
        except ValueError:  # expired between add and incr
            cache.set(bucket, 1, timeout=window + 5)
            count = 1
    return count, window_start + window - now


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def too_many(request, retry_after):
    if request.path.startswith("/api/"):
        response = JsonResponse({"error": "Terlalu banyak percobaan. Coba lagi nanti.",
            "retry_after": retry_after}, status=429)
    else:
        response = HttpResponse(render_to_string("rate_limited.html",
            {"retry_minutes": max(1, round(retry_after / 60))}, request=request), status=429)
    response["Retry-After"] = str(retry_after)
    return response


def rate_limit(scope, *, limit, window, methods=("POST",), field=None):
    """Allow `limit` requests per `window` seconds per client IP for `methods`.

    With `field`, a second, separate budget applies to that submitted value (for example the
    login username), so one account cannot be guessed at from many addresses.
    """
    def decorator(view):
        @functools.wraps(view)
        def wrapped(request, *args, **kwargs):
            if settings.RATE_LIMIT_ENABLED and request.method in methods:
                store = getattr(getattr(request, "store", None), "id", "-")
                keys = [f"{scope}:{store}:ip:{_digest(client_ip(request))}"]
                value = request.POST.get(field, "").strip().lower() if field else ""
                if value:
                    keys.append(f"{scope}:{store}:{field}:{_digest(value)}")
                for key in keys:
                    count, retry_after = _hit(key, window)
                    if count > limit:
                        return too_many(request, retry_after)
            return view(request, *args, **kwargs)
        return wrapped
    return decorator
