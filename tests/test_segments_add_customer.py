"""The Pelanggan page can add a customer by hand (dialog → merchant-customer-new)."""
from django.contrib.auth.models import User
from django.test import Client, TestCase

from apps.shops.models import Customer, Store, StoreStaff


class AddCustomerTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("pemilik", password="x")
        self.store = Store.objects.create(owner=self.owner, name="Maju Jaya", slug="maju")
        self.client.force_login(self.owner)

    def test_the_page_offers_the_button_and_the_form_creates_an_active_customer(self):
        page = self.client.get("/books/customers/")
        self.assertContains(page, "Tambah pelanggan")
        self.assertNotContains(page, "CRM")
        reply = self.client.post("/books/customers/new/", {"name": "Bu Sari", "phone": "0812", "tags": "grosir",
            "company_name": "CV Sari", "accepts_marketing": "1"})
        customer = Customer.objects.get(store=self.store, name="Bu Sari")
        self.assertRedirects(reply, f"/books/customers/{customer.id}/", fetch_redirect_response=False)
        self.assertEqual((customer.status, customer.tags, customer.company_name, customer.accepts_marketing),
            ("active", "grosir", "CV Sari", True))

    def test_a_known_phone_opens_that_customer_instead_of_a_duplicate(self):
        first = Customer.objects.create(store=self.store, name="Ayu", phone="0813")
        reply = self.client.post("/books/customers/new/", {"name": "Ayu Lestari", "phone": "0813"})
        self.assertRedirects(reply, f"/books/customers/{first.id}/", fetch_redirect_response=False)
        self.assertEqual(Customer.objects.filter(store=self.store).count(), 1)

    def test_a_name_is_required_and_a_viewer_cannot_add(self):
        self.assertRedirects(self.client.post("/books/customers/new/", {"name": " "}), "/books/customers/",
            fetch_redirect_response=False)
        viewer = User.objects.create_user("pelihat", password="x")
        StoreStaff.objects.create(store=self.store, user=viewer, role="viewer")
        other = Client()
        other.force_login(viewer)
        self.assertEqual(other.post("/books/customers/new/", {"name": "X"}).status_code, 404)
        self.assertNotContains(other.get("/books/customers/"), "Tambah pelanggan")
        self.assertEqual(Customer.objects.filter(store=self.store).count(), 0)
