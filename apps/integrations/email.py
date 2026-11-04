"""Send email through SMTP2GO's HTTP API with an API key, as an alternative to SMTP:

    EMAIL_BACKEND=apps.integrations.email.Smtp2goBackend
    SMTP2GO_API_KEY=api-...

The sender address must be a verified sender (or on a verified domain) in SMTP2GO."""
import base64
import logging

import requests
from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger(__name__)


class Smtp2goBackend(BaseEmailBackend):
    timeout = 20

    def __init__(self, fail_silently=False, api_key=None, **kwargs):
        super().__init__(fail_silently=fail_silently, **kwargs)
        self.api_key = api_key or settings.SMTP2GO_API_KEY
        self.url = f"{settings.SMTP2GO_API_URL}/email/send"

    def send_messages(self, email_messages):
        if not email_messages:
            return 0
        if not self.api_key:
            if not self.fail_silently:
                raise RuntimeError("SMTP2GO_API_KEY is not configured.")
            return 0
        sent = 0
        for message in email_messages:
            try:
                if self._send(message):
                    sent += 1
            except Exception:
                logger.exception("SMTP2GO could not send %r", message.subject)
                if not self.fail_silently:
                    raise
        return sent

    @staticmethod
    def payload(message):
        recipients = message.to
        if not (recipients or message.cc or message.bcc):
            return None
        data = {"sender": message.from_email or settings.DEFAULT_FROM_EMAIL, "to": recipients,
            "subject": message.subject}
        if message.cc:
            data["cc"] = message.cc
        if message.bcc:
            data["bcc"] = message.bcc
        html = next((content for content, mimetype in getattr(message, "alternatives", [])
            if mimetype == "text/html"), None)
        if message.content_subtype == "html":
            data["html_body"] = message.body
        else:
            data["text_body"] = message.body
            if html:
                data["html_body"] = html
        headers = [{"header": "Reply-To", "value": ", ".join(message.reply_to)}] if message.reply_to else []
        headers += [{"header": name, "value": str(value)} for name, value in message.extra_headers.items()]
        if headers:
            data["custom_headers"] = headers
        attachments = []
        for attachment in message.attachments:
            if isinstance(attachment, tuple):
                filename, content, mimetype = attachment
                if isinstance(content, str):
                    content = content.encode()
                attachments.append({"filename": filename or "lampiran",
                    "fileblob": base64.b64encode(content).decode(),
                    "mimetype": mimetype or "application/octet-stream"})
        if attachments:
            data["attachments"] = attachments
        return data

    def _send(self, message):
        data = self.payload(message)
        if data is None:
            return False
        response = requests.post(self.url, json=data, timeout=(10, self.timeout),
            headers={"X-Smtp2go-Api-Key": self.api_key, "Accept": "application/json"})
        body = response.json() if "json" in response.headers.get("Content-Type", "") else {}
        result = body.get("data") or {}
        if not response.ok or not result.get("succeeded"):
            raise RuntimeError(f"SMTP2GO rejected the email (HTTP {response.status_code}): "
                f"{result.get('error') or result.get('failures') or response.text[:300]}")
        return True
