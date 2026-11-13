"""The sidebar shows a role only the links that role can open (apps/orders/nav_access.py)."""
import os
from pathlib import Path

from django.contrib.auth.models import User
from test_sales_docs import Base

from apps.orders import nav_access
from apps.orders.templatetags.merchant_nav import NAV, _url, link_key
from apps.shops import permissions
from apps.shops.models import StoreStaff

TARGET = Path(nav_access.__file__)


class NavAccessTests(Base):
    def links(self):
        for _section, groups in NAV:
            for group in groups:
                for link in group.links:
                    href = _url(link.url, link.args)
                    if href is not None:
                        yield link, href + (f"?{link.query}" if link.query else "")

    def opens(self, href):
        return self.client.get(href).status_code != 404

    def refused_by_role(self):
        self.client.force_login(self.owner)
        open_to_owner = [(link, href) for link, href in self.links() if self.opens(href)]
        refused = {}
        for role in permissions.ROLE_ORDER:
            user = User.objects.create_user("nav_" + role, password="x")
            StoreStaff.objects.create(store=self.store, user=user, role=role)
            self.client.force_login(user)
            refused[role] = sorted({link_key(link) for link, href in open_to_owner if not self.opens(href)})
        return refused

    def test_the_recorded_table_matches_the_server(self):
        refused = self.refused_by_role()
        if os.environ.get("WRITE_NAV_ACCESS"):
            head = TARGET.read_text().split("REFUSED = ")[0]
            body = "".join(f'    "{role}": frozenset({{\n' + "".join(f'        "{key}",\n' for key in keys) + "    }),\n"
                for role, keys in refused.items())
            TARGET.write_text(head + "REFUSED = {\n" + body + "}\n")
            return
        self.assertEqual({role: sorted(keys) for role, keys in nav_access.REFUSED.items()}, refused,
            "Access changed: regenerate with WRITE_NAV_ACCESS=1 pytest tests/test_nav_access.py")

    def labels(self, role, real):
        from apps.orders.templatetags.merchant_nav import build_nav
        nav = build_nav({"merchant_role": role, "merchant_real_role": real, "merchant_store": self.store})
        return {link["label"] for section in nav for group in section["groups"] for link in group["links"]}

    def test_a_role_sees_no_link_it_cannot_open(self):
        accountant = self.labels("finance", "bookkeeper")
        self.assertIn("Jurnal umum", accountant)
        self.assertIn("Tutup buku", accountant)
        self.assertNotIn("Impor pesanan", accountant)      # marketplace import: not an accountant's page
        self.assertNotIn("Paket langganan", accountant)
        owner = self.labels("admin", "owner")
        self.assertIn("Impor pesanan", owner)
        self.assertIn("Paket langganan", owner)
        self.assertLess(len(self.labels("viewer", "accountant")), len(owner))

    def test_the_page_passes_the_real_role_to_the_sidebar(self):
        user = User.objects.create_user("akuntan", password="x")
        StoreStaff.objects.create(store=self.store, user=user, role="bookkeeper")
        self.client.force_login(user)
        page = self.client.get("/merchant/accounting/journal/")
        self.assertEqual(page.context["merchant_real_role"], "bookkeeper")
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get("/merchant/accounting/journal/").context["merchant_real_role"], "owner")
