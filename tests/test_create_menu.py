"""The header "Buat baru" menu: every entry opens, dialog entries land on a page holding that dialog."""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from apps.orders.templatetags.merchant_nav import CREATE, _url
from apps.shops.models import Store, StoreStaff


class CreateMenuTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("pemilik", password="x")
        self.store = Store.objects.create(owner=self.owner, name="Maju Jaya", slug="maju")
        self.client.force_login(self.owner)

    def entries(self):
        for _title, items in CREATE:
            for label, name, _icon, args, query, _permission in items:
                yield label, _url(name, args) + (f"?{query}" if query else ""), query

    def test_every_entry_opens_for_the_owner_and_its_dialog_exists(self):
        for label, href, query in self.entries():
            reply = self.client.get(href, follow=True)
            self.assertEqual(reply.status_code, 200, (label, href))
            if query.startswith("buka="):
                self.assertContains(reply, f'<dialog id="{query[5:]}"', msg_prefix=label)

    def test_the_menu_is_grouped_and_covers_the_main_documents(self):
        html = self.client.get(reverse("merchant-dashboard")).content.decode()
        menu = html.split("create-menu")[1].split("</details>")[0]
        for title in ("Penjualan", "Pembelian", "Kas &amp; bank", "Persediaan", "Akuntansi"):
            self.assertIn(title, menu)
        for label in ("Invoice", "Nota kredit", "Pesanan pembelian", "Nota debit", "Transfer antar rekening",
                "Stok opname", "Jurnal umum", "Aset tetap", "Pelanggan", "Supplier"):
            self.assertIn(f"<span>{label}</span>", menu)

    def test_a_viewer_gets_no_entry_they_cannot_open(self):
        viewer = User.objects.create_user("pelihat", password="x")
        StoreStaff.objects.create(store=self.store, user=viewer, role="viewer")
        self.client.force_login(viewer)
        html = self.client.get(reverse("merchant-dashboard")).content.decode()
        menu = html.split("create-menu")[1].split("</details>")[0] if "create-menu" in html else ""
        for line in menu.split("<a "):
            if 'href="' not in line:
                continue
            href = line.split('href="')[1].split('"')[0]
            self.assertNotEqual(self.client.get(href, follow=True).status_code, 404, href)
