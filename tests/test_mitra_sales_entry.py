"""Penjualan mitra: what a mitra reports it sold, recorded once, priced from the mitra list, booked at settlement."""
from django.urls import reverse
from test_sales_docs import Base

from apps.orders.models import Order
from apps.settlements.models import ConsignmentPartner, PartnerProductPrice


class MitraSaleEntryTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)
        self.partner = ConsignmentPartner.objects.create(store=self.store, name="AB Shop")
        self.product = self.store.products.get(sku="KBL")
        PartnerProductPrice.objects.create(partner=self.partner, product=self.product, price=80_000)
        self.url = reverse("mitra-invoice-new", args=[self.partner.pk])

    def post(self, **extra):
        data = {"idem": "key-1", "invoice_date": self.today.isoformat(), "buyer_name": "AB Shop Nov",
            "sku[]": ["KBL"], "qty[]": ["3"], "unit_price[]": ["80000"], "discount": "0"}
        data.update(extra)
        return self.client.post(self.url, data)

    def test_form_offers_the_mitra_price(self):
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'data-price="80000"')

    def test_records_one_sell_through_order_even_when_sent_twice(self):
        self.post()
        self.post()
        orders = Order.objects.filter(store=self.store, consignment_partner=self.partner)
        self.assertEqual(orders.count(), 1)
        order = orders.get()
        self.assertEqual(order.order_type, "reseller_sell_through")
        self.assertEqual(order.total, 240_000)
        listing = self.client.get(reverse("mitra-invoice-list", args=[self.partner.pk]))
        self.assertContains(listing, order.invoice_number)

    def test_unknown_product_keeps_what_was_typed(self):
        page = self.post(**{"sku[]": ["TIDAKADA"], "idem": "key-2"})
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'value="TIDAKADA"')
        self.assertFalse(Order.objects.filter(consignment_partner=self.partner).exists())

    def test_discount_above_subtotal_is_refused(self):
        self.post(discount="999999", idem="key-3")
        self.assertFalse(Order.objects.filter(consignment_partner=self.partner).exists())
