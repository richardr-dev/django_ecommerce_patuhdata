"""Email through SMTP2GO's API with an API key."""
from unittest.mock import patch

from django.core import mail
from django.core.mail import EmailMultiAlternatives
from django.test import SimpleTestCase, override_settings

BACKEND = "apps.integrations.email.Smtp2goBackend"


class FakeResponse:
    def __init__(self, status=200, data=None):
        self.status_code, self.ok = status, status < 400
        self.headers = {"Content-Type": "application/json"}
        self._data = data if data is not None else {"data": {"succeeded": 1, "failed": 0}}
        self.text = str(self._data)

    def json(self):
        return self._data


@override_settings(EMAIL_BACKEND=BACKEND, SMTP2GO_API_KEY="api-test",
    DEFAULT_FROM_EMAIL="Lakuin <noreply@lakuin.store>")
class Smtp2goBackendTests(SimpleTestCase):
    @patch("apps.integrations.email.requests.post", return_value=FakeResponse())
    def test_sends_text_and_html_with_the_api_key(self, post):
        message = EmailMultiAlternatives("Pesanan diterima", "Halo Ayu", to=["ayu@example.com"],
            bcc=["arsip@lakuin.store"], reply_to=["toko@example.com"])
        message.attach_alternative("<p>Halo Ayu</p>", "text/html")
        message.attach("nota.txt", "isi", "text/plain")
        self.assertEqual(message.send(), 1)

        url, kwargs = post.call_args.args[0], post.call_args.kwargs
        self.assertEqual(url, "https://api.smtp2go.com/v3/email/send")
        self.assertEqual(kwargs["headers"]["X-Smtp2go-Api-Key"], "api-test")
        body = kwargs["json"]
        self.assertEqual(body["sender"], "Lakuin <noreply@lakuin.store>")
        self.assertEqual((body["to"], body["bcc"]), (["ayu@example.com"], ["arsip@lakuin.store"]))
        self.assertEqual((body["text_body"], body["html_body"]), ("Halo Ayu", "<p>Halo Ayu</p>"))
        self.assertEqual(body["custom_headers"], [{"header": "Reply-To", "value": "toko@example.com"}])
        self.assertEqual(body["attachments"][0]["filename"], "nota.txt")

    @patch("apps.integrations.email.requests.post", return_value=FakeResponse(400,
        {"data": {"error": "Sender not verified", "succeeded": 0}}))
    def test_a_rejected_email_raises_unless_silent(self, _post):
        with self.assertRaisesMessage(RuntimeError, "Sender not verified"):
            mail.send_mail("Tes", "Isi", None, ["ayu@example.com"])
        self.assertEqual(mail.send_mail("Tes", "Isi", None, ["ayu@example.com"],
            fail_silently=True), 0)

    @override_settings(SMTP2GO_API_KEY="")
    def test_a_missing_key_is_reported(self):
        with self.assertRaisesMessage(RuntimeError, "SMTP2GO_API_KEY"):
            mail.send_mail("Tes", "Isi", None, ["ayu@example.com"])
