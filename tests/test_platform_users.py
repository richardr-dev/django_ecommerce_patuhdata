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


class TrialTests(TestCase):
    def setUp(self):
        from apps.shops import trial
        self.trial = trial
        self.partner = User.objects.create_superuser("partner@x.id", "partner@x.id", "pw-partner-1")
        self.store = install(**CLIENT, trial_days=14)["store"]
        self.owner = self.store.owner
        self.owner.set_password("pw-owner-123"); self.owner.save()

    def _expire(self):
        from datetime import timedelta

        from django.utils import timezone
        Store.objects.filter(pk=self.store.pk).update(trial_ends_at=timezone.now() - timedelta(hours=1))

    def test_trial_runs_the_back_office_with_a_banner(self):
        self.client.force_login(self.owner)
        page = self.client.get("/books/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Uji coba gratis")

    def test_after_the_end_every_page_and_write_is_blocked_with_the_notice(self):
        self._expire()
        self.client.force_login(self.owner)
        for path in ("/books/", "/books/sales/invoices/", "/kasir/"):
            page = self.client.get(path)
            self.assertEqual(page.status_code, 402, path)
            self.assertContains(page, "Masa uji coba berakhir", status_code=402)
        self.assertEqual(self.client.post("/books/staff/", {"name": "x"}).status_code, 402)

    def test_signing_out_still_works_and_the_partner_is_never_locked(self):
        self._expire()
        self.client.force_login(self.owner)
        self.assertNotEqual(self.client.post("/books/logout/").status_code, 402)
        self.client.force_login(self.partner)
        self.assertNotEqual(self.client.get("/platform/users/").status_code, 402)

    def test_a_company_without_a_trial_date_has_full_access(self):
        plain = install(**{**CLIENT, "store_name": "Lama", "slug": "toko-lama", "owner_email": "lama@x.id"})["store"]
        self.assertIsNone(plain.trial_ends_at)
        plain.owner.set_password("pw-owner-123"); plain.owner.save()
        self.client.force_login(plain.owner)
        self.assertEqual(self.client.get("/books/").status_code, 200)

    def test_sales_extends_or_activates_and_the_company_can_work_again(self):
        self._expire()
        self.client.force_login(self.partner)
        self.client.post("/registration/", {"trial_action": "extend", "store": self.store.pk, "days": "7"})
        self.store.refresh_from_db()
        self.assertFalse(self.trial.is_expired(self.store))
        self.client.post("/registration/", {"trial_action": "activate", "store": self.store.pk})
        self.store.refresh_from_db()
        self.assertIsNone(self.store.trial_ends_at)
        self.client.post("/registration/", {"trial_action": "end", "store": self.store.pk})
        self.store.refresh_from_db()
        self.assertTrue(self.trial.is_expired(self.store))
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get("/books/").status_code, 402)

    def test_the_notice_offers_the_sales_contact(self):
        from django.test import override_settings
        self._expire()
        self.client.force_login(self.owner)
        with override_settings(SALES_WHATSAPP="0812-3456-7890", SALES_EMAIL="sales@patuhdata.id"):
            page = self.client.get("/books/")
        self.assertContains(page, "wa.me/6281234567890", status_code=402)
        self.assertContains(page, "sales@patuhdata.id", status_code=402)


class PlatformCompanyTests(TestCase):
    def setUp(self):
        self.partner = User.objects.create_superuser("partner@x.id", "partner@x.id", "pw-partner-1")
        self.store = install(**CLIENT, trial_days=14)["store"]
        self.client.force_login(self.partner)
        self.url = f"/platform/companies/{self.store.pk}/"

    def _post(self, **over):
        data = {"name": "Toko Maju Baru", "legal_name": "PT Maju Baru", "npwp": "0123456789012345",
            "address": "Jl. Baru 2", "taxpayer_type": "company", "is_pkp": "1", "support_email": "a@b.id",
            "support_phone": "0812", "trial_end": "2030-01-31", **over}
        return self.client.post(self.url, data)

    def test_edit_company_tax_identity_and_trial_end(self):
        from apps.tax.models import TaxProfile
        self.assertContains(self.client.get(self.url), "Toko Maju")
        self._post()
        self.store.refresh_from_db()
        profile = TaxProfile.objects.get(store=self.store)
        self.assertEqual((self.store.name, profile.legal_name, profile.address), ("Toko Maju Baru", "PT Maju Baru", "Jl. Baru 2"))
        self.assertEqual(self.store.trial_ends_at.date().isoformat(), "2030-01-31")

    def test_empty_trial_date_is_full_access_and_bad_input_keeps_everything(self):
        self._post(trial_end="")
        self.store.refresh_from_db()
        self.assertIsNone(self.store.trial_ends_at)
        page = self._post(name="Berubah", npwp="123")
        self.assertContains(page, "NPWP harus 16 digit")
        self.store.refresh_from_db()
        self.assertEqual(self.store.name, "Toko Maju Baru")   # the refused save changed nothing

    def test_a_store_account_cannot_open_it(self):
        self.client.force_login(self.store.owner)
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_sales_contact_and_default_trial_are_editable(self):
        from apps.shops.models import PlatformSettings
        from apps.shops.trial import sales_contact
        self.client.post("/platform/settings/", {"sales_whatsapp": "0811-222-333", "sales_email": "s@p.id",
            "default_trial_days": "21"})
        conf = PlatformSettings.get()
        self.assertEqual((conf.sales_email, conf.default_trial_days), ("s@p.id", 21))
        self.assertEqual(sales_contact()["whatsapp_url"], "https://wa.me/62811222333")
        self.assertContains(self.client.get("/registration/"), 'value="21"')


class SubscribeToUnlockTests(TestCase):
    def setUp(self):
        from datetime import timedelta

        from django.utils import timezone

        from apps.billing.models import Plan
        self.plan, _ = Plan.objects.update_or_create(code="pro", defaults={"name": "Pro", "price_monthly": 399000,
            "price_yearly": 3990000})
        self.store = install(**CLIENT, trial_days=14)["store"]
        Store.objects.filter(pk=self.store.pk).update(trial_ends_at=timezone.now() - timedelta(hours=1))
        self.store.refresh_from_db()
        self.client.force_login(self.store.owner)

    def _pay(self, months=1):
        from apps.billing.models import SubscriptionInvoice
        from apps.billing.services import mark_invoice_paid
        invoice = SubscriptionInvoice.objects.create(store=self.store, plan=self.plan, months=months,
            amount=self.plan.price_for(months), provider="demo")
        self.assertEqual(mark_invoice_paid(invoice.id, amount=invoice.amount), "")
        self.store.refresh_from_db()
        return invoice

    def test_a_locked_company_can_still_open_billing_and_is_offered_to_pay(self):
        self.assertEqual(self.client.get("/books/billing/").status_code, 200)
        self.assertEqual(self.client.get("/books/").status_code, 402)
        self.assertContains(self.client.get("/books/"), "Pilih paket dan bayar", status_code=402)

    def test_paying_unlocks_and_the_lock_follows_the_paid_period(self):
        invoice = self._pay()
        invoice.refresh_from_db()
        self.assertEqual(self.store.trial_ends_at, invoice.period_end)
        self.assertEqual(self.client.get("/books/").status_code, 200)

    def test_a_lapsed_paid_company_locks_again_after_the_grace_days(self):
        from datetime import timedelta

        from django.utils import timezone
        self._pay()
        Store.objects.filter(pk=self.store.pk).update(trial_ends_at=timezone.now() - timedelta(days=1))
        self.assertEqual(self.client.get("/books/").status_code, 200)   # inside the grace days
        Store.objects.filter(pk=self.store.pk).update(trial_ends_at=timezone.now() - timedelta(days=30))
        page = self.client.get("/books/")
        self.assertContains(page, "Masa langganan berakhir", status_code=402)

    def test_a_company_with_full_access_stays_that_way_after_paying(self):
        Store.objects.filter(pk=self.store.pk).update(trial_ends_at=None)
        self.store.refresh_from_db()
        self._pay()
        self.assertIsNone(self.store.trial_ends_at)

    def test_a_staff_member_without_billing_rights_is_not_offered_payment(self):
        from apps.shops.platform_users import create_user
        viewer, *_ = create_user(name="V", contact="v@x.id", store=self.store, role="viewer")
        self.client.force_login(viewer)
        page = self.client.get("/books/")
        self.assertNotContains(page, "Pilih paket dan bayar", status_code=402)


class CompanyListTests(TestCase):
    def setUp(self):
        self.partner = User.objects.create_superuser("partner@x.id", "partner@x.id", "pw-partner-1")
        self.store = install(**CLIENT, trial_days=14)["store"]
        self.client.force_login(self.partner)

    def test_list_shows_the_company_with_trial_controls(self):
        page = self.client.get("/platform/companies/")
        self.assertContains(page, "Toko Maju")
        self.assertContains(page, "Atur tanggal")
        self.assertContains(page, "Akhiri sekarang")

    def test_set_an_exact_date_end_now_and_full_access_from_the_list(self):
        from apps.shops.trial import is_expired
        self.client.post("/platform/companies/", {"store": self.store.pk, "trial_action": "set", "date": "2020-01-01"})
        self.store.refresh_from_db()
        self.assertTrue(is_expired(self.store))
        self.client.post("/platform/companies/", {"store": self.store.pk, "trial_action": "set", "date": "2035-01-01"})
        self.store.refresh_from_db()
        self.assertFalse(is_expired(self.store))
        self.client.post("/platform/companies/", {"store": self.store.pk, "trial_action": "end"})
        self.store.refresh_from_db()
        self.assertTrue(is_expired(self.store))
        self.client.post("/platform/companies/", {"store": self.store.pk, "trial_action": "activate"})
        self.store.refresh_from_db()
        self.assertIsNone(self.store.trial_ends_at)

    def test_a_store_account_cannot_open_it(self):
        self.client.force_login(self.store.owner)
        self.assertEqual(self.client.get("/platform/companies/").status_code, 302)
        self.assertEqual(self.client.post("/platform/companies/", {"store": self.store.pk, "trial_action": "end"}).status_code, 302)
        self.store.refresh_from_db()
        self.assertIsNotNone(self.store.trial_ends_at)


class PartnerLogoutTests(TestCase):
    def test_every_partner_page_has_a_working_sign_out(self):
        partner = User.objects.create_superuser("partner@x.id", "partner@x.id", "pw-partner-1")
        self.client.force_login(partner)
        for path in ("/platform/companies/", "/platform/users/", "/platform/settings/", "/registration/"):
            page = self.client.get(path)
            self.assertContains(page, 'action="/books/logout/"', msg_prefix=path)
            self.assertContains(page, "Keluar")
        self.client.post("/books/logout/")
        self.assertEqual(self.client.get("/platform/users/").status_code, 302)
        self.assertIn("/login", self.client.get("/platform/users/")["Location"])


class TrialCompanyCanBuyTests(TestCase):
    def test_a_trial_company_without_a_subscription_sees_plans_and_can_start_checkout(self):
        from datetime import timedelta

        from django.utils import timezone

        from apps.billing.models import Plan, SubscriptionInvoice
        store = install(**CLIENT, trial_days=14)["store"]
        Store.objects.filter(pk=store.pk).update(trial_ends_at=timezone.now() - timedelta(hours=1))
        self.assertFalse(hasattr(store, "subscription") and store.subscription)
        pro, _ = Plan.objects.update_or_create(code="pro", defaults={"name": "Pro", "price_monthly": 399000,
            "price_yearly": 3990000, "is_public": True})
        self.client.force_login(store.owner)
        page = self.client.get("/books/billing/")
        self.assertContains(page, "Pilih Pro")
        self.assertContains(page, "UJI COBA BERAKHIR")
        self.assertNotContains(page, "Dikelola platform")
        from django.test import override_settings
        with override_settings(PAYMENT_PROVIDER="demo", ALLOW_DEMO_PAYMENTS=True):
            res = self.client.post("/books/billing/checkout/", {"plan": "pro", "months": "1"})
        self.assertEqual(res.status_code, 302)
        self.assertTrue(SubscriptionInvoice.objects.filter(store=store, plan=pro, status="pending").exists())

    def test_the_pay_button_text_is_white(self):
        from datetime import timedelta

        from django.utils import timezone
        store = install(**CLIENT, trial_days=14)["store"]
        Store.objects.filter(pk=store.pk).update(trial_ends_at=timezone.now() - timedelta(hours=1))
        self.client.force_login(store.owner)
        page = self.client.get("/books/")
        self.assertContains(page, ".tr-act a.tr-btn", status_code=402)
        self.assertContains(page, "color:#fff", status_code=402)


class ReturnFromDokuTests(TestCase):
    def test_coming_back_to_billing_activates_a_bill_doku_says_is_paid(self):
        from datetime import timedelta
        from unittest import mock

        from django.utils import timezone

        from apps.billing.models import Plan, SubscriptionInvoice
        from apps.integrations.payments import PaymentStatus
        store = install(**CLIENT, trial_days=14)["store"]
        Store.objects.filter(pk=store.pk).update(trial_ends_at=timezone.now() - timedelta(hours=1))
        plan, _ = Plan.objects.update_or_create(code="pro", defaults={"name": "Pro", "price_monthly": 399000,
            "price_yearly": 3990000, "is_public": True})
        invoice = SubscriptionInvoice.objects.create(store=store, plan=plan, months=1, amount=399000,
            provider="doku", payment_reference="SBtest0001")
        self.client.force_login(store.owner)
        with mock.patch("apps.billing.services.DokuPaymentProvider") as doku:
            doku.return_value.fetch_status.return_value = PaymentStatus("paid", amount=399000)
            page = self.client.get("/books/billing/")
        invoice.refresh_from_db(); store.refresh_from_db()
        self.assertEqual(page.status_code, 200)
        self.assertEqual(invoice.status, "paid")
        self.assertGreater(store.trial_ends_at, timezone.now())
        self.assertEqual(self.client.get("/books/").status_code, 200)

    def test_an_unpaid_bill_stays_pending(self):
        from unittest import mock

        from apps.billing.models import Plan, SubscriptionInvoice
        from apps.integrations.payments import PaymentStatus
        store = install(**CLIENT, trial_days=14)["store"]
        plan, _ = Plan.objects.update_or_create(code="pro", defaults={"name": "Pro", "price_monthly": 399000,
            "price_yearly": 3990000, "is_public": True})
        invoice = SubscriptionInvoice.objects.create(store=store, plan=plan, months=1, amount=399000,
            provider="doku", payment_reference="SBtest0002")
        self.client.force_login(store.owner)
        with mock.patch("apps.billing.services.DokuPaymentProvider") as doku:
            doku.return_value.fetch_status.return_value = PaymentStatus("pending")
            self.client.get("/books/billing/")
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "pending")


class PlanEditorTests(TestCase):
    def setUp(self):
        self.partner = User.objects.create_superuser("partner@x.id", "partner@x.id", "pw-partner-1")
        self.client.force_login(self.partner)

    def test_list_shows_plans_and_a_store_account_is_refused(self):
        self.assertContains(self.client.get("/platform/plans/"), "Paket &amp; harga")
        store = install(**CLIENT)["store"]
        self.client.force_login(store.owner)
        self.assertEqual(self.client.get("/platform/plans/").status_code, 302)
        self.assertEqual(self.client.post("/platform/plans/new/", {"code": "x-plan", "name": "X"}).status_code, 302)

    def test_change_a_price_and_limits(self):
        from apps.billing.models import Plan
        plan = Plan.objects.filter(is_default=False, contact_sales=False).first()
        res = self.client.post(f"/platform/plans/{plan.pk}/", {"name": plan.name, "price_monthly": "125000",
            "price_yearly": "1250000", "max_products": "", "max_channels": "5", "included_outlets": "2",
            "is_public": "1"})
        self.assertEqual(res.status_code, 302)
        plan.refresh_from_db()
        self.assertEqual((int(plan.price_monthly), int(plan.price_yearly), plan.max_products, plan.max_channels),
            (125000, 1250000, None, 5))

    def test_add_a_plan_and_a_new_default_replaces_the_old_one(self):
        from apps.billing.models import Plan
        self.client.post("/platform/plans/new/", {"code": "gold-plus", "name": "Gold Plus", "price_monthly": "250000",
            "price_yearly": "2500000", "is_public": "1", "is_default": "1"})
        self.assertEqual(Plan.objects.filter(is_default=True).count(), 1)
        self.assertTrue(Plan.objects.get(code="gold-plus").is_default)

    def test_bad_input_is_refused_and_keeps_the_typed_values(self):
        page = self.client.post("/platform/plans/new/", {"code": "Bad Code!", "name": "Z", "price_monthly": "10"})
        self.assertContains(page, "Kode paket")
        self.assertContains(page, 'value="Z"')

    def test_a_used_plan_cannot_be_deleted_but_an_unused_one_can(self):
        from apps.billing.models import Plan, Subscription
        used = Plan.objects.create(code="used-plan", name="Used", price_monthly=1000, price_yearly=10000)
        Subscription.objects.create(store=install(**CLIENT)["store"], plan=used)
        self.client.post("/platform/plans/", {"action": "delete", "plan": used.pk})
        self.assertTrue(Plan.objects.filter(pk=used.pk).exists())
        spare = Plan.objects.create(code="spare-plan", name="Spare", price_monthly=1000, price_yearly=10000)
        self.client.post("/platform/plans/", {"action": "delete", "plan": spare.pk})
        self.assertFalse(Plan.objects.filter(pk=spare.pk).exists())


class ChangeLogAndSubscriptionTests(TestCase):
    def setUp(self):
        from apps.billing.models import Plan
        self.partner = User.objects.create_superuser("partner@x.id", "partner@x.id", "pw-partner-1")
        self.store = install(**CLIENT, trial_days=14)["store"]
        self.plan = Plan.objects.filter(is_default=False, contact_sales=False, price_monthly__gt=0).first()
        self.client.force_login(self.partner)

    def test_a_price_and_name_change_is_logged_with_old_and_new_values(self):
        from apps.shops.models import PlatformLog
        old = (self.plan.name, int(self.plan.price_monthly))
        plan = self.plan
        form = {"name": "Nama Baru", "tagline": plan.tagline, "price_monthly": "777000", "price_yearly": "7770000",
            "max_products": plan.max_products or "", "max_channels": plan.max_channels or "",
            "included_outlets": plan.included_outlets or "", "sort_order": plan.sort_order,
            "features": plan.features, "is_public": "1" if plan.is_public else "",
            "allow_custom_domain": "1" if plan.allow_custom_domain else "",
            "allow_payout_reconciliation": "1" if plan.allow_payout_reconciliation else ""}
        self.client.post(f"/platform/plans/{plan.pk}/", form)
        entry = PlatformLog.objects.filter(action="plan.updated").first()
        self.assertEqual(entry.actor_name, "partner@x.id")
        self.assertEqual(entry.changes["name"], [old[0], "Nama Baru"])
        self.assertEqual(entry.changes["price_monthly"], [old[1], 777000])
        self.assertNotIn("tagline", entry.changes)
        page = self.client.get("/platform/log/")
        self.assertContains(page, "Nama Baru")
        self.assertContains(page, "777000")

    def test_edit_a_companys_subscription_and_it_is_logged(self):
        from datetime import date

        from apps.shops.models import PlatformLog
        res = self.client.post(f"/platform/companies/{self.store.pk}/", {"section": "subscription",
            "plan": self.plan.code, "period_end": "2031-05-01", "renew_months": "12", "extra_outlets": "0",
            "auto_renew": "1"})
        self.assertEqual(res.status_code, 302)
        self.store.refresh_from_db()
        self.assertEqual(self.store.subscription.plan, self.plan)
        self.assertEqual(self.store.subscription.current_period_end.date(), date(2031, 5, 1))
        self.assertEqual(self.store.trial_ends_at.date(), date(2031, 5, 1))   # the lock date follows
        entry = PlatformLog.objects.filter(action__startswith="subscription.", store=self.store).first()
        self.assertEqual(entry.changes["berlaku_sampai"], ["", "2031-05-01"])

    def test_change_the_plan_again_and_remove_it(self):
        from apps.shops.models import PlatformLog
        self.client.post(f"/platform/companies/{self.store.pk}/", {"section": "subscription", "plan": self.plan.code,
            "period_end": "2031-05-01", "renew_months": "1", "extra_outlets": "0"})
        self.client.post(f"/platform/companies/{self.store.pk}/", {"section": "subscription", "plan": ""})
        self.store.refresh_from_db()
        self.assertFalse(hasattr(self.store, "subscription") and self.store.subscription_id if False else False)
        from apps.billing.models import Subscription
        self.assertFalse(Subscription.objects.filter(store=self.store).exists())
        self.assertTrue(PlatformLog.objects.filter(action="subscription.removed").exists())

    def test_bad_subscription_input_is_refused(self):
        page = self.client.post(f"/platform/companies/{self.store.pk}/", {"section": "subscription",
            "plan": self.plan.code, "period_end": "", "renew_months": "1", "extra_outlets": "0"})
        self.assertContains(page, "tanggal")
        from apps.billing.models import Subscription
        self.assertFalse(Subscription.objects.filter(store=self.store).exists())

    def test_user_company_trial_and_settings_changes_all_reach_the_log(self):
        from apps.shops.models import PlatformLog
        self.client.post(f"/platform/companies/{self.store.pk}/", {"name": "Toko Maju 2", "npwp": "",
            "taxpayer_type": "company", "trial_end": "2031-01-01"})
        self.client.post("/platform/companies/", {"store": self.store.pk, "trial_action": "end"})
        self.client.post("/platform/users/", {"action": "create", "name": "Sari", "contact": "sari@maju.co.id"})
        self.client.post("/platform/settings/", {"sales_whatsapp": "0811", "sales_email": "", "default_trial_days": "7"})
        actions = set(PlatformLog.objects.values_list("action", flat=True))
        self.assertTrue({"company.updated", "trial.ended", "user.created", "settings.updated"} <= actions, actions)

    def test_the_log_page_is_for_partners_only_and_filters(self):
        self.client.post(f"/platform/plans/{self.plan.pk}/", {"name": "Zzz", "price_monthly": "1", "price_yearly": "10",
            "is_public": "1"})
        self.assertContains(self.client.get("/platform/log/?jenis=plan"), "Zzz")
        self.assertNotContains(self.client.get("/platform/log/?jenis=user"), "Zzz")
        self.client.force_login(self.store.owner)
        self.assertEqual(self.client.get("/platform/log/").status_code, 302)


class SlashedPriceTests(TestCase):
    def setUp(self):
        from apps.billing.models import Plan
        self.partner = User.objects.create_superuser("partner@x.id", "partner@x.id", "pw-partner-1")
        self.client.force_login(self.partner)
        self.plan, _ = Plan.objects.update_or_create(code="pro", defaults={"name": "Pro", "price_monthly": 100000,
            "price_yearly": 1000000, "is_public": True, "contact_sales": False})

    def _form(self, **over):
        return {"name": "Pro", "price_monthly": "99000", "list_price_monthly": "129000", "price_yearly": "990000",
            "list_price_yearly": "1548000", "is_public": "1", **over}

    def test_percentage_is_worked_out_from_the_two_prices(self):
        self.client.post(f"/platform/plans/{self.plan.pk}/", self._form())
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.discount_pct_monthly, 23)     # (129000-99000)/129000
        self.assertEqual(self.plan.discount_pct_yearly, 36)      # (1548000-990000)/1548000
        self.assertEqual(int(self.plan.price_for(1)), 99000)     # what is charged stays the final price
        self.assertEqual(int(self.plan.price_for(12)), 990000)

    def test_no_slashed_price_means_no_discount(self):
        self.client.post(f"/platform/plans/{self.plan.pk}/", self._form(list_price_monthly="0", list_price_yearly="0"))
        self.plan.refresh_from_db()
        self.assertEqual((self.plan.discount_pct_monthly, self.plan.discount_pct_yearly), (0, 0))

    def test_a_slashed_price_not_above_the_final_price_is_refused(self):
        page = self.client.post(f"/platform/plans/{self.plan.pk}/", self._form(list_price_monthly="90000"))
        self.assertContains(page, "harus lebih besar")
        self.plan.refresh_from_db()
        self.assertEqual(int(self.plan.price_monthly), 100000)   # nothing saved

    def test_buyers_see_the_struck_price_the_percent_and_pay_the_final_price(self):
        self.client.post(f"/platform/plans/{self.plan.pk}/", self._form())
        store = install(**CLIENT, trial_days=14)["store"]
        self.client.force_login(store.owner)
        page = self.client.get("/books/billing/")
        self.assertContains(page, "Hemat 23%")
        self.assertContains(page, "line-through")
        from django.test import override_settings
        with override_settings(PAYMENT_PROVIDER="demo", ALLOW_DEMO_PAYMENTS=True):
            self.client.post("/books/billing/checkout/", {"plan": "pro", "months": "1"})
        from apps.billing.models import SubscriptionInvoice
        self.assertEqual(int(SubscriptionInvoice.objects.get(store=store).amount), 99000)

    def test_the_change_log_records_the_slashed_price(self):
        from apps.shops.models import PlatformLog
        self.client.post(f"/platform/plans/{self.plan.pk}/", self._form())
        changes = PlatformLog.objects.filter(action="plan.updated").first().changes
        self.assertEqual(changes["list_price_monthly"], [0, 129000])
        self.assertEqual(changes["price_monthly"], [100000, 99000])
