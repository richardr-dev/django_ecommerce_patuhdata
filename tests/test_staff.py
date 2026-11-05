"""Staf & akses: invite by email or WhatsApp with a role, a one-time password link, role
changes and removal, and only the owner or a store admin can manage the team."""
import re

from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, TestCase, override_settings

from apps.shops.models import Store, StoreStaff


@override_settings(PLATFORM_DOMAIN="")
class StaffTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner@toko.id", email="owner@toko.id", password="x")
        self.store = Store.objects.create(owner=self.owner, name="Ruuma", slug="ruuma")
        self.seller = Client()
        self.seller.force_login(self.owner)

    def _invite(self, contact, role="warehouse", name="Budi"):
        return self.seller.post("/merchant/staff/", {"name": name, "contact": contact, "role": role})

    def _link(self, page):
        return re.search(r'/merchant/invite/[^/"]+/[^/"]+/', page.content.decode()).group(0)

    def test_invite_by_whatsapp_sets_a_password_once_and_signs_in(self):
        page = self._invite("0812-3456-7890")
        member = StoreStaff.objects.get(store=self.store)
        self.assertEqual((member.user.username, member.role), ("081234567890", "warehouse"))
        self.assertFalse(member.user.has_usable_password())
        self.assertContains(page, "Kirim via WhatsApp")
        link = self._link(page)
        newcomer = Client()
        reply = newcomer.post(link, {"new_password1": "Rendang-Enak-88", "new_password2": "Rendang-Enak-88"})
        self.assertRedirects(reply, "/merchant/", fetch_redirect_response=False)
        self.assertContains(Client().get(link), "Link tidak berlaku")  # single use
        login = Client().post("/merchant/login/", {"username": "081234567890",
            "password": "Rendang-Enak-88", "next": "/merchant/"})
        self.assertEqual(login.status_code, 302)

    def test_invite_by_email_sends_the_link_and_existing_accounts_are_just_added(self):
        self._invite("Ani@Example.com", role="finance", name="Ani")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("/merchant/invite/", mail.outbox[0].body)
        existing = User.objects.create_user("lama@example.com", email="lama@example.com", password="y")
        self._invite("lama@example.com", role="viewer")
        self.assertEqual(StoreStaff.objects.get(user=existing).role, "viewer")
        self.assertEqual(len(mail.outbox), 1)

    def test_owner_changes_roles_and_removes_staff(self):
        self._invite("0812-3456-7890")
        member = StoreStaff.objects.get(store=self.store)
        self.seller.post(f"/merchant/staff/{member.id}/", {"role": "manager"})
        member.refresh_from_db()
        self.assertEqual(member.role, "manager")
        self.seller.post(f"/merchant/staff/{member.id}/", {"action": "remove"})
        self.assertFalse(StoreStaff.objects.exists())

    def test_only_owner_or_admin_manage_staff(self):
        clerk = User.objects.create_user("clerk")
        StoreStaff.objects.create(store=self.store, user=clerk, role="manager")
        client = Client()
        client.force_login(clerk)
        self.assertEqual(client.get("/merchant/staff/").status_code, 404)
        StoreStaff.objects.filter(user=clerk).update(role="admin")
        self.assertEqual(client.get("/merchant/staff/").status_code, 200)
        self.assertContains(self._invite("bukan-email"), "Tulis email atau nomor WhatsApp")
