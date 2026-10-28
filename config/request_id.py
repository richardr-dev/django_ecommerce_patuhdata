"""Per-request correlation IDs for logs and support tickets."""
import contextvars
import logging
import re
import uuid

_request_id = contextvars.ContextVar("request_id", default="-")
# Accept an upstream ID (load balancer, mobile app) only if it cannot inject into log lines.
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{8,64}$")


class RequestIDMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.headers.get("X-Request-ID", "")
        request_id = incoming if _SAFE_ID.match(incoming) else uuid.uuid4().hex
        request.request_id = request_id
        token = _request_id.set(request_id)
        try:
            response = self.get_response(request)
        finally:
            _request_id.reset(token)
        response["X-Request-ID"] = request_id
        return response


class RequestIDFilter(logging.Filter):
    """Adds %(request_id)s to every record; "-" outside a request (worker, commands)."""

    def filter(self, record):
        record.request_id = _request_id.get()
        return True
