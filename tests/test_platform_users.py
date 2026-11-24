"""The partner's user page: add, switch off, delete; nobody else may open it."""
from django.contrib.auth.models import User
from django.test import TestCase

from apps.shops.models import Store, StoreStaff
from apps.shops.platform_users import PlatformUserError, create_user, delete_user, set_active
from apps.shops.provisioning import install

CLIENT = {"store_name": "Toko Maju", "slug": "toko-maju", "owner_name": "Budi", "owner_email": "budi@maju.co.id"}


class PlatformUserTests(TestCase):
    def setUp(self):
        self.partner = User.objects.create_superuser("partner@x.id", "partner@x.id", "pw-partner-1")
        self.store = install(**CLIENT)["store"]

    def test_add_a_person_into_a_company_with_a_role(self):
        user, created, link = create_user(name="Sari", contact="Sari@Maju.co.id", store=self.store,
            role="finance", actor=self.partner)
        self.assertTrue(created)
        self.assertEqual(user.username, "sari@maju.co.id")
        self.assertIn("/books/invite/", link)
        self.assertEqual(StoreStaff.objects.get(store=self.store, user=user).role, "finance")

    def test_a_bad_contact_or_role_is_refused(self):
        with self.assertRaises(PlatformUserError):
            create_user(name="X", contact="not-a-contact")
        with self.assertRaises(PlatformUserError):
            create_user(name="X", contact="x@y.id", store=self.store, role="")

    def test_an_owner_cannot_be_deleted_but_can_be_switched_off(self):
        with self.assertRaises(PlatformUserError):
            delete_user(self.store.owner, self.partner)
        self.assertTrue(User.objects.filter(pk=self.store.owner_id).exists())
        set_active(self.store.owner, False, self.partner)
        self.store.owner.refresh_from_db()
        self.assertFalse(self.store.owner.is_active)

    def test_delete_removes_a_member_and_their_membership(self):
        user, *_ = create_user(name="Sari", contact="sari@maju.co.id", store=self.store, role="viewer")
        delete_user(user, self.partner)
        self.assertFalse(User.objects.filter(pk=user.pk).exists())
        self.assertFalse(StoreStaff.objects.filter(store=self.store).exists())

    def test_nobody_edits_themselves_or_another_partner(self):
        other = User.objects.create_superuser("o@x.id", "o@x.id", "pw-other-1")
        for target in (self.partner, other):
            with self.assertRaises(PlatformUserError):
                delete_user(target, self.partner)
            with self.assertRaises(PlatformUserError):
                set_active(target, False, self.partner)

    def test_only_a_superuser_opens_the_page(self):
        self.client.force_login(self.store.owner)
        self.assertEqual(self.client.get("/platform/users/").status_code, 302)
        self.client.post("/platform/users/", {"action": "create", "name": "Z", "contact": "z@z.id"})
        self.assertFalse(User.objects.filter(username="z@z.id").exists())

    def test_the_page_adds_switches_off_and_deletes(self):
        self.client.force_login(self.partner)
        page = self.client.post("/platform/users/", {"action": "create", "name": "Sari",
            "contact": "sari@maju.co.id", "store": self.store.pk, "role": "viewer"})
        self.assertContains(page, "/books/invite/")
        sari = User.objects.get(username="sari@maju.co.id")
        self.assertContains(self.client.get("/platform/users/?q=sari"), "sari@maju.co.id")
        self.client.post("/platform/users/", {"action": "deactivate", "user": sari.pk})
        sari.refresh_from_db()
        self.assertFalse(sari.is_active)
        self.client.post("/platform/users/", {"action": "delete", "user": sari.pk})
        self.assertFalse(User.objects.filter(pk=sari.pk).exists())
        self.assertTrue(Store.objects.filter(pk=self.store.pk).exists())


class PlatformUserUpdateTests(TestCase):
    def setUp(self):
        from apps.shops.platform_users import create_user
        self.partner = User.objects.create_superuser("partner@x.id", "partner@x.id", "pw-partner-1")
        self.store = install(**CLIENT)["store"]
        self.other = install(**{**CLIENT, "store_name": "Toko Dua", "slug": "toko-dua",
            "owner_email": "dua@x.id"})["store"]
        self.sari, *_ = create_user(name="Sari", contact="sari@maju.co.id", store=self.store, role="viewer")

    def test_edit_name_and_login(self):
        from apps.shops.platform_users import update_user
        update_user(self.sari, name="Sari Dewi", contact="sari.dewi@maju.co.id", actor=self.partner)
        self.sari.refresh_from_db()
        self.assertEqual((self.sari.first_name, self.sari.username, self.sari.email),
            ("Sari Dewi", "sari.dewi@maju.co.id", "sari.dewi@maju.co.id"))

    def test_a_login_used_by_someone_else_is_refused(self):
        from apps.shops.platform_users import update_user
        with self.assertRaises(PlatformUserError):
            update_user(self.sari, name="Sari", contact="budi@maju.co.id", actor=self.partner)

    def test_change_role_add_to_another_company_and_remove(self):
        from apps.shops.platform_users import remove_membership, set_membership
        set_membership(self.sari, self.store, "finance", self.partner)
        set_membership(self.sari, self.other, "viewer", self.partner)
        self.assertEqual(StoreStaff.objects.get(store=self.store, user=self.sari).role, "finance")
        self.assertEqual(StoreStaff.objects.filter(user=self.sari).count(), 2)
        self.assertTrue(remove_membership(self.sari, self.other, self.partner))
        self.assertEqual(StoreStaff.objects.filter(user=self.sari).count(), 1)

    def test_detail_page_saves_and_issues_a_link(self):
        self.client.force_login(self.partner)
        url = f"/platform/users/{self.sari.pk}/"
        self.assertContains(self.client.get(url), "sari@maju.co.id")
        self.client.post(url, {"action": "save", "name": "Sari B", "contact": "sari.b@maju.co.id"})
        self.sari.refresh_from_db()
        self.assertEqual(self.sari.username, "sari.b@maju.co.id")
        self.assertContains(self.client.post(url, {"action": "link"}), "/books/invite/")

    def test_detail_page_is_for_partners_only(self):
        self.client.force_login(self.store.owner)
        self.assertEqual(self.client.get(f"/platform/users/{self.sari.pk}/").status_code, 302)
