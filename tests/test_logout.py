"""Keluar ends the session for good: back to the merchant login (not the Django admin), nothing
left in the browser, private pages never cached, and app tokens end with the password."""
from datetime import timedelta

from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from apps.orders.models import MobileAccessToken
from apps.orders.tokens import prune_sign_ins
from apps.shops.auth import SELLER_COOKIE
from apps.shops.models import Store


@override_settings(PLATFORM_DOMAIN="")
class LogoutTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("sri@example.com", password="Sambal-Matah-77")
        self.store = Store.objects.create(owner=self.owner, name="Toko", slug="toko")
        self.seller = Client()
        self.seller.force_login(self.owner)

    def test_keluar_returns_to_the_merchant_login_and_clears_the_browser(self):
        dashboard = self.seller.get("/merchant/")
        self.assertContains(dashboard, 'action="/merchant/logout/"')
        self.assertNotContains(dashboard, "/platform-admin/logout/")
        reply = self.seller.post("/merchant/logout/")
        self.assertEqual(reply["Location"], "/merchant/login/?keluar=1")
        self.assertEqual(reply["Clear-Site-Data"], '"cache", "storage", "cookies"')
        for name in ("sessionid", "csrftoken", SELLER_COOKIE):
            self.assertEqual(reply.cookies[name].value, "", name)
        self.assertFalse(Session.objects.exists())
        self.assertRedirects(self.seller.get("/merchant/"), "/merchant/login/?next=/merchant/")
        self.assertContains(self.seller.get("/merchant/login/?keluar=1"), "Anda sudah keluar")

    def test_a_link_cannot_sign_anyone_out(self):
        self.assertRedirects(self.seller.get("/merchant/logout/"), "/merchant/",
            fetch_redirect_response=False)
        self.assertEqual(self.seller.get("/merchant/").status_code, 200)

    def test_back_office_pages_are_never_cached(self):
        cache_control = self.seller.get("/merchant/")["Cache-Control"]
        self.assertIn("no-store", cache_control)
        self.assertIn("private", cache_control)

    def test_customer_logout_keeps_other_stores_cookies(self):
        reply = self.seller.post("/account/logout/")
        self.assertEqual(reply["Location"], "/")
        self.assertEqual(reply["Clear-Site-Data"], '"cache", "storage"')
        self.assertFalse(Session.objects.exists())

    def _token(self, **extra):
        return MobileAccessToken.objects.create(store=self.store, user=self.owner,
            token_hash=f"h{MobileAccessToken.objects.count()}",
            expires_at=timezone.now() + timedelta(days=30), **extra)

    def test_app_tokens_end_when_the_password_changes_or_the_account_is_closed(self):
        token = self._token()
        self.owner.last_login = timezone.now()
        self.owner.save(update_fields=["last_login"])
        token.refresh_from_db()
        self.assertIsNone(token.revoked_at)
        self.owner.set_password("Rendang-Baru-88")
        self.owner.save()
        token.refresh_from_db()
        self.assertIsNotNone(token.revoked_at)
        second = self._token()
        self.owner.is_active = False
        self.owner.save()
        second.refresh_from_db()
        self.assertIsNotNone(second.revoked_at)

    def test_old_sessions_and_tokens_are_pruned(self):
        old = timezone.now() - timedelta(days=40)
        stale = self._token()
        MobileAccessToken.objects.filter(pk=stale.pk).update(expires_at=old)
        live = self._token()
        Session.objects.update(expire_date=old)
        prune_sign_ins()
        self.assertEqual(list(MobileAccessToken.objects.values_list("pk", flat=True)), [live.pk])
        self.assertFalse(Session.objects.exists())
