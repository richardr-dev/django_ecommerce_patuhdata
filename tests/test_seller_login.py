"""Sellers without email open a store and sign in with their WhatsApp number, and a signed-in
seller sees a "Kelola toko" link on their own storefront."""
from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings

from apps.shops.auth import SELLER_COOKIE
from apps.shops.models import Store

SIGNUP = {"store_name": "Toko Bu Sri", "slug": "bu-sri", "phone": "0812-3456-7890",
    "full_name": "Sri Wahyuni", "password1": "Sambal-Matah-77", "password2": "Sambal-Matah-77",
    "accept_terms": "on"}


@override_settings(PLATFORM_DOMAIN="")
class SellerLoginTests(TestCase):
    def _login(self, username, password="Sambal-Matah-77"):
        client = Client()
        reply = client.post("/merchant/login/", {"username": username, "password": password,
            "next": "/merchant/"})
        return client, reply

    def _seller(self, username="sri@example.com", phone="081234567890", slug="bu-sri"):
        user = User.objects.create_user(username, email=username if "@" in username else "",
            password="Sambal-Matah-77")
        Store.objects.create(owner=user, name="Toko", slug=slug, support_phone=phone)
        return user

    def test_signup_without_email_uses_the_whatsapp_number_to_sign_in(self):
        reply = Client().post("/start/", SIGNUP)
        self.assertEqual(reply.status_code, 302)
        user = Store.objects.get(slug="bu-sri").owner
        self.assertEqual((user.username, user.email), ("081234567890", ""))
        for typed in ("081234567890", "+62 812-3456-7890", "6281234567890"):
            _, login = self._login(typed)
            self.assertEqual(login.status_code, 302, typed)
            self.assertEqual(login["Location"], "/merchant/")

    def test_a_used_number_cannot_open_a_second_email_less_account(self):
        self._seller(username="081234567890")
        reply = Client().post("/start/", {**SIGNUP, "slug": "bu-sri-2"})
        self.assertEqual(reply.status_code, 200)
        self.assertContains(reply, "Nomor ini sudah dipakai")

    def test_older_accounts_sign_in_with_their_store_number(self):
        self._seller(phone="0812 3456 7890")
        _, reply = self._login("081234567890")
        self.assertEqual(reply.status_code, 302)
        _, reply = self._login("SRI@example.com")
        self.assertEqual(reply.status_code, 302)

    def test_wrong_password_or_shared_number_does_not_sign_in(self):
        self._seller()
        _, reply = self._login("081234567890", password="salah")
        self.assertContains(reply, "Email/nomor WhatsApp atau kata sandi salah")
        self._seller(username="other@example.com", slug="lain")  # same number, another owner
        _, reply = self._login("081234567890")
        self.assertEqual(reply.status_code, 200)

    def test_the_login_page_explains_whatsapp_sign_in(self):
        page = Client().get("/merchant/login/")
        self.assertContains(page, "Email atau nomor WhatsApp")

    def test_owner_sees_kelola_toko_on_their_storefront_only(self):
        self._seller()
        client, _ = self._login("081234567890")
        client.get("/merchant/")
        self.assertIn(SELLER_COOKIE, client.cookies)
        self.assertTrue(client.cookies[SELLER_COOKIE]["httponly"])
        self.assertContains(client.get("/"), "Kelola toko")
        self.assertNotContains(Client().get("/"), "Kelola toko")
        forged = Client()
        forged.cookies[SELLER_COOKIE] = "[1]"
        self.assertNotContains(forged.get("/"), "Kelola toko")
        client.post("/account/logout/")
        self.assertEqual(client.cookies[SELLER_COOKIE].value, "")
