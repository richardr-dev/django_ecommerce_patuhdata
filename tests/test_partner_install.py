"""Stores are installed by a partner (command or superuser page); nobody registers from the web."""
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.accounting.models import Account, BankAccount
from apps.catalog.models import Warehouse
from apps.shops.models import AuditLog, Organization, Store
from apps.shops.provisioning import InstallError, install
from apps.tax.models import TaxProfile

CLIENT = {"store_name": "Toko Maju", "slug": "toko-maju", "owner_name": "Budi Santoso",
    "owner_email": "budi@tokomaju.co.id", "owner_phone": "081234567890", "legal_name": "PT Maju Jaya",
    "npwp": "0123456789012345", "address": "Jl. Sudirman 1, Jakarta", "is_pkp": True}


class InstallServiceTests(TestCase):
    def test_one_call_leaves_a_store_ready_for_bookkeeping(self):
        result = install(**CLIENT)
        store, owner = result["store"], result["owner"]
        self.assertEqual(owner.username, "budi@tokomaju.co.id")
        self.assertFalse(owner.has_usable_password())
        self.assertIn("/merchant/invite/", result["invite_link"])
        self.assertEqual(store.organization.name, "PT Maju Jaya")
        self.assertEqual(store.organization.owner, owner)
        self.assertTrue(Warehouse.objects.filter(store=store, is_active=True).exists())
        self.assertTrue(Account.objects.filter(store=store, code="1100").exists())
        self.assertTrue(Account.objects.filter(store=store, code="3300").exists())
        self.assertTrue(BankAccount.objects.filter(store=store).exists())
        profile = TaxProfile.objects.get(store=store)
        self.assertEqual((profile.legal_name, profile.npwp, profile.is_pkp), ("PT Maju Jaya", "0123456789012345", True))
        self.assertTrue(AuditLog.objects.filter(store=store, action="store.installed").exists())

    def test_the_owner_can_set_a_password_from_the_link_and_sign_in(self):
        result = install(**CLIENT)
        path = "/" + result["invite_link"].split("/", 3)[3]
        self.assertEqual(self.client.get(path).status_code, 200)

    def test_a_second_brand_joins_the_clients_organization(self):
        first = install(**CLIENT)["store"]
        second = install(**{**CLIENT, "store_name": "Maju Outlet", "slug": "maju-outlet"},
            organization=first.organization)
        self.assertEqual(second["store"].organization, first.organization)
        self.assertFalse(second["owner_created"])
        self.assertEqual(Organization.objects.count(), 1)
        self.assertEqual(User.objects.count(), 1)

    def test_a_brand_cannot_be_added_to_someone_elses_organization(self):
        first = install(**CLIENT)["store"]
        with self.assertRaises(InstallError):
            install(**{**CLIENT, "slug": "lain", "owner_email": "lain@x.co", "owner_phone": ""},
                organization=first.organization)

    def test_bad_input_is_refused_and_leaves_nothing_behind(self):
        for change in ({"slug": "A"}, {"slug": "admin"}, {"owner_email": "", "owner_phone": ""}, {"npwp": "123"},
                {"npwp": "", "is_pkp": True}, {"store_name": " "}, {"owner_name": ""}, {"owner_phone": "12"}):
            with self.assertRaises(InstallError, msg=str(change)):
                install(**{**CLIENT, **change})
        install(**CLIENT)
        with self.assertRaises(InstallError):
            install(**CLIENT)                                   # the code is taken
        self.assertEqual(Store.objects.count(), 1)
        self.assertEqual(User.objects.count(), 1)


class InstallCommandTests(TestCase):
    def test_command_installs_and_prints_the_password_link(self):
        out = StringIO()
        call_command("install_store", name="Toko Maju", code="toko-maju", owner="Budi", email="budi@x.co",
            legal_name="PT Maju", npwp="0123456789012345", pkp=True, stdout=out)
        self.assertIn("terpasang", out.getvalue())
        self.assertIn("/merchant/invite/", out.getvalue())
        self.assertTrue(Store.objects.filter(slug="toko-maju").exists())

    def test_command_reports_problems_as_errors(self):
        with self.assertRaises(CommandError):
            call_command("install_store", name="X", code="!!", owner="B", email="b@x.co")
        with self.assertRaises(CommandError):
            call_command("install_store", name="X", code="toko-x", owner="B", email="b@x.co", organization="tidak-ada")


class InstallPageTests(TestCase):
    def setUp(self):
        self.partner = User.objects.create_superuser("partner", "p@x.co", "pw")
        self.owner = install(**CLIENT)["owner"]

    def test_only_a_platform_superuser_sees_it(self):
        self.assertEqual(self.client.get("/instalasi/").status_code, 302)        # signed out: to the login
        self.client.force_login(self.owner)
        shown = self.client.get("/instalasi/", follow=True)                      # a store owner: sent back, told why
        self.assertRedirects(shown, "/merchant/")
        self.assertContains(shown, "akun partner")
        self.assertEqual(self.client.post("/instalasi/", {"store_name": "X", "slug": "xx-toko", "owner_name": "A",
            "owner_email": "a@x.co"}).status_code, 302)
        self.assertFalse(Store.objects.filter(slug="xx-toko").exists())
        self.client.force_login(self.partner)
        self.assertContains(self.client.get("/instalasi/"), "Pasang toko untuk klien")

    def test_partner_installs_a_store_and_gets_the_link(self):
        self.client.force_login(self.partner)
        page = self.client.post("/instalasi/", {"store_name": "Kopi Senja", "slug": "kopi-senja", "owner_name": "Sari",
            "owner_phone": "081298765432", "taxpayer_type": "individual"})
        self.assertContains(page, "terpasang")
        self.assertContains(page, "/merchant/invite/")
        store = Store.objects.get(slug="kopi-senja")
        self.assertEqual(store.owner.username, "081298765432")
        self.assertEqual(AuditLog.objects.get(store=store, action="store.installed").actor, self.partner)

    def test_an_error_keeps_what_was_typed(self):
        self.client.force_login(self.partner)
        page = self.client.post("/instalasi/", {"store_name": "Kopi Senja", "slug": "toko-maju", "owner_name": "Sari",
            "owner_email": "sari@x.co"})
        self.assertContains(page, "sudah dipakai")
        self.assertContains(page, 'value="Kopi Senja"')


class NoPublicRegistrationTests(TestCase):
    def test_the_old_registration_and_marketing_addresses_are_gone(self):
        for path in ("/start/", "/stores/", "/ajak-teman/", "/merchant/brands/new/", "/r/toko-maju"):
            self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_the_login_page_offers_no_registration(self):
        page = self.client.get("/merchant/login/")
        self.assertNotContains(page, "Daftar")
        self.assertContains(page, "partner")


class PartnerLandingTests(TestCase):
    def test_a_partner_without_a_store_lands_on_the_install_page_after_login(self):
        User.objects.create_superuser("partner", "p@x.co", "pw")
        response = self.client.post("/merchant/login/", {"username": "partner", "password": "pw", "next": "/merchant/"})
        self.assertRedirects(response, "/merchant/", fetch_redirect_response=False)
        self.assertRedirects(self.client.get("/merchant/"), "/instalasi/", fetch_redirect_response=False)

    def test_a_superuser_who_owns_a_store_still_sees_the_dashboard(self):
        owner = install(**CLIENT)["owner"]
        owner.is_superuser = True
        owner.save()
        self.client.force_login(owner)
        self.assertEqual(self.client.get("/merchant/").status_code, 200)
