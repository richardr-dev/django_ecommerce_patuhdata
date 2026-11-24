"""Aturan stok minimum and Usulan pembelian.

Worked example: Kabel has 100 on the shelf, 20 still on order from CV Sumber (last price 58.000) and 30 promised
(and reserved) on an open sales order. Forecast 100 − 30 + 20 = 90, below the minimum of 120: order up to 200, i.e. 110,
rounded up to the dus of 12 = 120, from CV Sumber at 58.000. Once that order exists the forecast is 210 and
nothing is proposed. Lampu (100 on hand, minimum 10) needs nothing; Lampu in Toko Depan (5 there, minimum 10,
maximum 30) needs 25, on a purchase order bound for Toko Depan."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.urls import reverse
from test_sales_docs import Base

from apps.accounting import purchasing, reordering
from apps.accounting.models import PurchaseOrder, ReorderRule, Supplier
from apps.catalog.models import InventoryLevel, ProductVariant, Warehouse
from apps.shops.models import StoreStaff
from apps.tax import sales_orders

D = Decimal


class ReorderTests(Base):
    def setUp(self):
        super().setUp()
        self.sumber = Supplier.objects.create(store=self.store, name="CV Sumber")
        self.toko = Warehouse.objects.create(store=self.store, code="TK", name="Toko Depan", priority=200)
        InventoryLevel.objects.create(warehouse=self.toko, product=self.lampu, on_hand=5)
        InventoryLevel.objects.filter(product=self.lampu, warehouse__code="G1").update(on_hand=95)
        purchasing.create_po(self.store, supplier=self.sumber, order_date=self.today - timedelta(days=3),
            lines=[{"sku": "KBL", "qty": 20, "unit_cost": D(58_000)}])
        sales_orders.create_sales_order(self.store, lines=[{"product": self.kabel, "qty": 30,
            "unit_price": D(100_000)}], buyer_name="Budi", customer=self.customer)
        self.kabel_rule = reordering.save_rule(self.store, product=self.kabel, minimum=120, maximum=200, multiple=12,
            lead_days=5)
        self.lampu_rule = reordering.save_rule(self.store, product=self.lampu, minimum=10, maximum=50)
        self.toko_rule = reordering.save_rule(self.store, product=self.lampu, warehouse=self.toko, minimum=10,
            maximum=30, supplier=self.sumber)

    def test_the_worked_example(self):
        where = reordering.position(self.kabel_rule)   # the sales order holds its 30: reserved, not counted again
        self.assertEqual((where.on_hand, where.reserved, where.incoming, where.outgoing, where.forecast),
            (100, 30, 20, 0, 90))
        found = {s.rule.pk: s for s in reordering.suggestions(self.store)}
        self.assertEqual(set(found), {self.kabel_rule.pk, self.toko_rule.pk})
        kabel = found[self.kabel_rule.pk]
        self.assertEqual((kabel.quantity, kabel.supplier, kabel.unit_cost), (120, self.sumber, D(58_000)))
        toko = found[self.toko_rule.pk]
        self.assertEqual((toko.position.on_hand, toko.position.incoming, toko.quantity), (5, 0, 25))
        made = reordering.order(self.store, [(kabel.rule, kabel.quantity, kabel.supplier, kabel.unit_cost),
            (toko.rule, toko.quantity, toko.supplier, toko.unit_cost)], order_date=self.today)
        self.assertEqual(len(made), 2)                                    # one per destination
        main = next(po for po in made if po.warehouse is None)
        self.assertEqual((main.expected_on, main.lines.get().quantity), (self.today + timedelta(days=5), 120))
        self.assertEqual(next(po for po in made if po.warehouse == self.toko).lines.get().quantity, 25)
        self.assertEqual(reordering.position(self.kabel_rule).forecast, 210)
        self.assertEqual(reordering.suggestions(self.store), [])
        self.assertEqual(InventoryLevel.objects.get(product=self.kabel).on_hand, 100)   # an order moves nothing

    def test_rules_are_checked(self):
        with self.assertRaisesMessage(reordering.ReorderError, "maksimum"):
            reordering.save_rule(self.store, product=self.kabel, warehouse=self.toko, minimum=10, maximum=5)
        with self.assertRaisesMessage(reordering.ReorderError, "sudah ada"):
            reordering.save_rule(self.store, product=self.kabel, minimum=1, maximum=5)
        ProductVariant.objects.create(product=self.lampu, name="Putih", sku="LMP-P")
        with self.assertRaisesMessage(reordering.ReorderError, "per varian"):
            reordering.save_rule(self.store, product=self.lampu, warehouse=self.toko, minimum=1, maximum=5)
        with self.assertRaisesMessage(reordering.ReorderError, "supplier"):
            reordering.order(self.store, [(self.lampu_rule, 5, None, D(1))], order_date=self.today)

    def test_a_variant_rule_counts_that_size(self):
        from apps.orders.stock import receive_goods
        kaos = self.kabel.__class__.objects.create(store=self.store, sku="KAOS", name="Kaos", slug="kaos", price=80_000)
        m = ProductVariant.objects.create(product=kaos, name="M", sku="KAOS-M", cost_price=D(40_000))
        receive_goods(store=self.store, product=kaos, variant=m, quantity=3, unit_cost=D(40_000))
        rule = reordering.save_rule(self.store, product=kaos, variant=m, minimum=5, maximum=12, supplier=self.sumber)
        suggestion = next(s for s in reordering.suggestions(self.store) if s.rule == rule)
        self.assertEqual((suggestion.position.on_hand, suggestion.quantity, suggestion.unit_cost), (3, 9, D(40_000)))
        po = reordering.order(self.store, [(rule, 9, self.sumber, D(40_000))], order_date=self.today)[0]
        self.assertEqual(po.lines.get().variant, m)


class ReorderPageTests(ReorderTests):
    url = reverse("merchant-reorder")

    def test_the_page_orders_once_and_beranda_asks(self):
        self.client.force_login(self.owner)
        home = self.client.get(reverse("merchant-dashboard"))
        self.assertContains(home, "barang di bawah stok minimum perlu dipesan")
        page = self.client.get(self.url)
        self.assertContains(page, "Kabel")
        self.assertEqual(len(page.context["suggestions"]), 2)
        data = {"action": "order", "idem": "r1", "pick": [str(self.kabel_rule.pk)],
            f"qty_{self.kabel_rule.pk}": "120", f"supplier_{self.kabel_rule.pk}": str(self.sumber.pk),
            f"cost_{self.kabel_rule.pk}": "58.000"}
        reply = self.client.post(self.url, data)
        po = PurchaseOrder.objects.exclude(notes="").get()
        self.assertRedirects(reply, reverse("merchant-purchase-order", args=[po.pk]), fetch_redirect_response=False)
        self.assertEqual((po.lines.get().quantity, po.lines.get().unit_cost), (120, D(58_000)))
        self.client.post(self.url, data)                                   # pressed twice
        self.assertEqual(PurchaseOrder.objects.count(), 2)                 # the setUp order and this one
        self.assertEqual(len(self.client.get(self.url).context["suggestions"]), 1)

    def test_rules_are_added_changed_and_removed_on_the_page(self):
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(self.url + "?tab=aturan"), "Toko Depan")
        self.client.post(self.url, {"action": "save_rule", "item": str(self.lampu.pk), "warehouse": "",
            "minimum": "20", "maximum": "60", "multiple": "1", "rule": str(self.lampu_rule.pk)})
        self.lampu_rule.refresh_from_db()
        self.assertEqual((self.lampu_rule.minimum, self.lampu_rule.maximum), (20, 60))
        page = self.client.post(self.url, {"action": "save_rule", "item": str(self.kabel.pk), "minimum": "9",
            "maximum": "3"}, follow=True)
        self.assertContains(page, "maksimum tidak boleh lebih kecil")
        self.client.post(self.url, {"action": "delete_rule", "rule": str(self.lampu_rule.pk)})
        self.assertFalse(ReorderRule.objects.filter(pk=self.lampu_rule.pk).exists())

    def test_a_viewer_sees_but_does_not_order(self):
        viewer = User.objects.create_user("ro-viewer")
        StoreStaff.objects.create(store=self.store, user=viewer, role="viewer")
        self.client.force_login(viewer)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertNotContains(self.client.get(self.url), "Buat pesanan pembelian")
        reply = self.client.post(self.url, {"action": "delete_rule", "rule": str(self.lampu_rule.pk)})
        self.assertIn(reply.status_code, {403, 404})
        self.assertTrue(ReorderRule.objects.filter(pk=self.lampu_rule.pk).exists())
