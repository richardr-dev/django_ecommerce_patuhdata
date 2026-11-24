"""Lupa kata sandi: the four reset pages are the sign-in card (no shop layout), and the email names the platform."""
import re

from django.conf import settings
from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase
from django.urls import reverse


class PasswordResetPagesTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("owner-reset", email="owner@perusahaan.test", password="lama-sekali-123")

    def test_the_whole_reset_runs_on_the_sign_in_card(self):
        form = self.client.get(reverse("password-reset"))
        self.assertContains(form, "css/login.css")
        self.assertContains(form, "Lupa kata sandi?")
        self.assertContains(form, reverse("merchant-login"))
        self.assertNotContains(form, "storefront")

        done = self.client.post(reverse("password-reset"), {"email": "owner@perusahaan.test"}, follow=True)
        self.assertContains(done, "Periksa email Anda")
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertIn(settings.PLATFORM_NAME, message.subject)
        self.assertIn(settings.PLATFORM_NAME, message.body)

        link = re.search(r"https?://[^/\s]+(/account/reset/\S+)", message.body).group(1)
        confirm = self.client.get(link, follow=True)
        self.assertContains(confirm, "Buat kata sandi baru")
        short = self.client.post(confirm.redirect_chain[-1][0], {"new_password1": "123", "new_password2": "123"})
        self.assertContains(short, "ln-err")                            # the rule is shown under the field
        complete = self.client.post(confirm.redirect_chain[-1][0], {"new_password1": "Glacier-baru-2026",
            "new_password2": "Glacier-baru-2026"}, follow=True)
        self.assertContains(complete, "Kata sandi diperbarui")
        self.assertTrue(User.objects.get(pk=self.user.pk).check_password("Glacier-baru-2026"))

    def test_a_used_link_says_so_and_offers_a_new_one(self):
        page = self.client.get(reverse("password-reset-confirm", args=["MQ", "set-password"]))
        self.assertContains(page, "Tautan tidak berlaku")
        self.assertContains(page, reverse("password-reset"))
