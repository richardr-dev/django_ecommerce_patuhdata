"""Menu ringkas: the sidebar starts short (everyday pages), "Menu lengkap" shows everything, the page you are on
is never hidden, and every page stays in the menu search."""
import re

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from apps.orders.templatetags.merchant_nav import CORE, NAV
from apps.shops.models import Store, StoreStaff


def nav_html(page):
    html = page.content.decode()
    return html.split('id="pd-nav"')[1].split("</nav>")[0]


def visible_labels(nav):
    """Links a person sees in the short menu: those not marked nav-extra."""
    return [label.strip() for cls, label in re.findall(r'class="(nav-link[^"]*)"[^>]*>\s*<span>([^<]+)</span>', nav)
            if "nav-extra" not in cls]


class SimpleMenuTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("nav-owner")
        self.store = Store.objects.create(owner=self.owner, name="Toko", slug="toko-nav")
        self.client = Client()
        self.client.force_login(self.owner)

    def test_every_core_label_names_a_real_link(self):
        labels = {(group.label, link.label) for _title, groups in NAV for group in groups for link in group.links}
        for group, names in CORE.items():
            for name in names:
                self.assertIn((group, name), labels, f"{group} › {name}")

    def test_the_short_menu_shows_everyday_pages_and_keeps_the_rest_in_the_page(self):
        nav = nav_html(self.client.get(reverse("merchant-dashboard")))
        shown = visible_labels(nav)
        self.assertIn("Invoice penjualan", shown)
        self.assertIn("Impor pesanan", shown)
        self.assertNotIn("Jurnal penyesuaian", shown)
        self.assertIn("Jurnal penyesuaian", nav)                  # still there for the menu search
        self.assertLess(len(shown), 35)
        self.assertIn('data-default="simple"', nav)
        self.assertIn("Tampilkan menu lengkap", nav)

    def test_the_page_you_are_on_is_never_hidden(self):
        nav = nav_html(self.client.get(reverse("merchant-adjustments")))
        self.assertIn("Jurnal penyesuaian", visible_labels(nav))
        group = re.search(r'<details class="nav-group([^"]*)" open>\s*<summary>.*?Buku besar', nav, re.S)
        self.assertIsNotNone(group)
        self.assertNotIn("nav-extra", group.group(1))

    def test_an_accountant_starts_on_the_full_menu(self):
        person = User.objects.create_user("nav-acc")
        StoreStaff.objects.create(store=self.store, user=person, role="accountant")
        self.client.force_login(person)
        self.assertIn('data-default="full"', nav_html(self.client.get(reverse("merchant-dashboard"))))
