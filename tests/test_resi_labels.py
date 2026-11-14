"""Unggah label resi (PDF) and the marketplace cards on Pesanan."""
import io
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase

from apps.catalog.models import InventoryLevel, Product, Warehouse
from apps.channels.adapters import MarketplaceOrder, OrderLine
from apps.channels.models import SalesChannel
from apps.channels.services import import_orders
from apps.orders import resi_labels
from apps.orders.models import Order
from apps.shops.models import AuditLog, Store, StoreStaff


def make_pdf(pages):
    """A real PDF with one page per list of text lines (so pypdf extracts text exactly as from a label)."""
    def esc(line):
        return line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    objects = [None, None, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    page_ids = []
    for lines in pages:
        text = "BT /F1 11 Tf 30 560 Td 16 TL " + " ".join(f"({esc(line)}) Tj T*" for line in lines) + " ET"
        stream = text.encode("latin-1")
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        content_id = len(objects)
        objects.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 420 600] /Contents %d 0 R "
            b"/Resources << /Font << /F1 3 0 R >> >> >>" % content_id)
        page_ids.append(len(objects))
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % i for i in page_ids) + b"] /Count %d >>" % len(page_ids)
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objects) + 1, xref)
    return out


SHOPEE_PAGE = ["SU2-TBG-D", "ECO", "No. Resi: 004685080641", "Penerima: Pembeli Uji", "Berat: 2000 gr",
    "No. Pesanan: 261107NGDU9Q...", "1 ZENKO+ Max SEPATU HITAM,40 1", "Pesan: (261107NGDU9QCQ)", "004685080641", "004685080641"]


class LabelReaderTests(TestCase):
    def test_shopee_label_gives_the_resi_and_the_full_order_number(self):
        label, = resi_labels.read_labels(io.BytesIO(make_pdf([SHOPEE_PAGE])))
        self.assertEqual(label.resi, "004685080641")
        self.assertIn("261107NGDU9QCQ", label.refs)
        self.assertEqual(label.cut_ref, "261107NGDU9Q")           # the printed one is cut short
        self.assertEqual(label.courier_code, "")                  # the courier is a logo, not text

    def test_the_most_repeated_code_is_the_resi_when_nothing_labels_it(self):
        page = ["Order ID: 5868351234567890", "JX1234567890", "JX1234567890", "JX1234567890", "Total 25000"]
        label, = resi_labels.read_labels(io.BytesIO(make_pdf([page])))
        self.assertEqual(label.resi, "JX1234567890")
        self.assertEqual(label.refs, ["5868351234567890"])

    def test_a_courier_named_on_the_label_is_read(self):
        page = ["J&T Express", "No. Resi: JP1234567890", "Order ID: 5868351234567890"]
        label, = resi_labels.read_labels(io.BytesIO(make_pdf([page])))
        self.assertEqual(label.courier_code, "jnt")

    def test_courier_codes_from_marketplace_shipping_options_and_resi_shapes(self):
        self.assertEqual(resi_labels.courier_code_from_text("JNE YES"), "jne")
        self.assertEqual(resi_labels.courier_code_from_text("Ninja Xpress"), "ninja")
        self.assertEqual(resi_labels.courier_code_from_text("SiCepat REG"), "sicepat")
        self.assertEqual(resi_labels.courier_code_from_text("Hemat Kargo"), "")
        self.assertEqual(resi_labels.courier_code_from_text("Pengiriman jnet"), "")       # not inside another word
        self.assertEqual(resi_labels.guess_courier_from_resi("004685080641"), "sicepat")
        self.assertEqual(resi_labels.guess_courier_from_resi("CM05459265352"), "jne")
        self.assertEqual(resi_labels.guess_courier_from_resi("JY91008038038"), "")        # ambiguous: no guess

    def test_files_that_are_not_readable_labels_say_why(self):
        for blob, reason in [(b"this is not a pdf", "bukan PDF"), (b"", "bukan PDF")]:
            with self.assertRaises(resi_labels.LabelError) as caught:
                resi_labels.read_labels(io.BytesIO(blob))
            self.assertIn(reason, str(caught.exception))
        empty_page, = resi_labels.read_labels(io.BytesIO(make_pdf([[]])))
        self.assertFalse(empty_page.readable)


class MarketplaceFixtures(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("seller", password="x")
        self.store = Store.objects.create(owner=self.owner, name="Toko", slug="toko-resi")
        self.product = Product.objects.create(store=self.store, name="Kopi", slug="kopi", sku="KOPI", price=15000)
        InventoryLevel.objects.create(warehouse=Warehouse.objects.create(store=self.store, name="G", code="G1"),
            product=self.product, on_hand=50)
        self.shopee = SalesChannel.objects.create(store=self.store, platform="shopee", name="Toko Shopee")
        self.client = Client()
        self.client.force_login(self.owner)

    def sale(self, external_id, status="Selesai", courier="", resi="", sku="KOPI", buyer="Budi", channel=None):
        data = MarketplaceOrder(external_id=external_id, buyer_name=buyer, phone="0812", address="Bandung",
            status_text=status, courier=courier, tracking_number=resi,
            lines=[OrderLine(sku=sku, quantity=1, unit_price=Decimal(15000))])
        import_orders(channel or self.shopee, [data], finished_only=True)
        return Order.objects.get(store=self.store, external_order_id=external_id)

    def upload(self, pages, client=None):
        pdf = SimpleUploadedFile("label.pdf", make_pdf(pages), content_type="application/pdf")
        return (client or self.client).post("/books/orders/label-resi/", {"action": "read", "file": pdf})


class LabelUploadTests(MarketplaceFixtures):
    def test_the_preview_matches_a_cut_off_order_number_and_saves_nothing(self):
        order = self.sale("261107NGDU9QCQ")
        page = self.upload([SHOPEE_PAGE])
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "004685080641")
        self.assertContains(page, "Siap disimpan")
        self.assertContains(page, f"#{str(order.id)[:8]}")
        self.assertContains(page, "tebakan")                       # SiCepat guessed from the resi shape
        order.refresh_from_db()
        self.assertEqual(order.tracking_number, "")               # nothing is stored by the preview

    def test_saving_sets_the_resi_and_the_courier_and_logs_it(self):
        order = self.sale("261107NGDU9QCQ")
        reply = self.client.post("/books/orders/label-resi/", {"action": "save", "n": "1", "use_0": "1",
            "resi_0": "004685080641", "order_0": str(order.id), "courier_0": "sicepat"})
        self.assertEqual((reply.status_code, reply["Location"]), (302, "/books/orders/?tab=all&sumber=semua"))
        order.refresh_from_db()
        self.assertEqual((order.tracking_number, order.courier_code, order.shipping_service), ("004685080641", "sicepat", "SiCepat"))
        self.assertTrue(AuditLog.objects.filter(store=self.store, action="order.tracking_updated",
            details__source="label_pdf").exists())

    def test_an_unticked_row_and_a_missing_courier_are_not_saved(self):
        order = self.sale("261107NGDU9QCQ")
        self.client.post("/books/orders/label-resi/", {"action": "save", "n": "1", "resi_0": "004685080641",
            "order_0": str(order.id), "courier_0": "sicepat"})                                  # not ticked
        self.client.post("/books/orders/label-resi/", {"action": "save", "n": "1", "use_0": "1", "resi_0": "004685080641",
            "order_0": str(order.id), "courier_0": ""})                                         # no courier
        order.refresh_from_db()
        self.assertEqual(order.tracking_number, "")

    def test_the_courier_already_on_the_order_wins_over_a_guess(self):
        self.sale("261107NGDU9QCQ", courier="SiCepat REG")
        page = self.upload([SHOPEE_PAGE])
        self.assertContains(page, "dari pesanan")
        self.assertNotContains(page, "tebakan")

    def test_a_different_existing_resi_needs_an_explicit_tick(self):
        order = self.sale("261107NGDU9QCQ", courier="SiCepat REG", resi="OLD123456789")
        page = self.upload([SHOPEE_PAGE])
        self.assertContains(page, "Resi berbeda")
        self.assertNotContains(page, " checked")                  # not ticked by default
        self.client.post("/books/orders/label-resi/", {"action": "save", "n": "1", "use_0": "1", "resi_0": "004685080641",
            "order_0": str(order.id), "courier_0": "sicepat"})
        order.refresh_from_db()
        self.assertEqual(order.tracking_number, "004685080641")

    def test_unknown_orders_unreadable_pages_and_duplicates_are_explained(self):
        order = self.sale("261107NGDU9QCQ")
        page = self.upload([SHOPEE_PAGE, SHOPEE_PAGE, ["No. Resi: 009999999999", "Order ID: 7777777777777"], ["x"]])
        self.assertContains(page, "Label ganda")
        self.assertContains(page, "Pesanan tidak ketemu")
        self.assertContains(page, "Tidak ada teks")
        self.assertEqual(Order.objects.get(pk=order.pk).tracking_number, "")

    def test_a_cancelled_order_gets_no_resi(self):
        self.sale("261107NGDU9QCQ", status="Dibatalkan")
        self.assertContains(self.upload([SHOPEE_PAGE]), "Tidak bisa diisi")

    def test_an_order_of_another_store_is_never_touched(self):
        other_owner = User.objects.create_user("other", password="x")
        other = Store.objects.create(owner=other_owner, name="Lain", slug="toko-lain")
        Product.objects.create(store=other, name="Kopi", slug="kopi", sku="KOPI", price=15000)
        channel = SalesChannel.objects.create(store=other, platform="shopee", name="Toko Shopee")
        data = MarketplaceOrder(external_id="261107NGDU9QCQ", buyer_name="X", phone="0", address="-", status_text="Selesai",
            lines=[OrderLine(sku="KOPI", quantity=1, unit_price=Decimal(1000))])
        import_orders(channel, [data], soft=True)
        foreign = Order.objects.get(store=other)
        self.assertContains(self.upload([SHOPEE_PAGE]), "Pesanan tidak ketemu")
        self.client.post("/books/orders/label-resi/", {"action": "save", "n": "1", "use_0": "1", "resi_0": "004685080641",
            "order_0": str(foreign.id), "courier_0": "sicepat"})
        foreign.refresh_from_db()
        self.assertEqual(foreign.tracking_number, "")

    def test_only_the_people_who_may_type_a_resi_can_open_the_page(self):
        for username, role, allowed in [("ops", "ops", True), ("gudang", "warehouse", True), ("cs", "customer_service", False),
                ("pelihat", "viewer", False), ("auditor", "accountant", False), ("akuntan", "bookkeeper", False)]:
            user = User.objects.create_user(username, password="x")
            StoreStaff.objects.create(store=self.store, user=user, role=role)
            client = Client()
            client.force_login(user)
            self.assertEqual(client.get("/books/orders/label-resi/").status_code, 200 if allowed else 404, role)

    def test_a_bad_upload_says_what_is_wrong(self):
        bad = SimpleUploadedFile("x.pdf", b"not a pdf", content_type="application/pdf")
        self.assertContains(self.client.post("/books/orders/label-resi/", {"action": "read", "file": bad}), "bukan PDF")
        self.assertContains(self.client.post("/books/orders/label-resi/", {"action": "read"}), "Pilih file PDF")


class OrdersPageTests(MarketplaceFixtures):
    def page(self, query=""):
        return self.client.get("/books/orders/" + query)

    def test_a_marketplace_card_shows_courier_resi_and_follows_the_marketplace_status(self):
        self.sale("MP1", courier="JNE YES", resi="JY91008038038")
        html = self.page("?tab=all").content.decode()
        card = html.split("<article")[1].split("</article>")[0]
        for expected in ("JNE YES", "JY91008038038", "Salin", "Status mengikuti Shopee", "Selesai, tidak ada langkah berikutnya"):
            self.assertIn(expected, card)
        for unwanted in ("Belum siap dikirim", "Cetak label", "Kemas pesanan", 'class="pick"'):
            self.assertNotIn(unwanted, card)
        self.assertNotIn("· 0812", card)                              # a username is not a phone number

    def test_a_marketplace_order_without_a_resi_offers_to_add_one(self):
        self.sale("MP2")
        html = self.page("?tab=all").content.decode()
        self.assertIn("Belum ada resi", html)
        self.assertIn('class="mk-resi"', html)
        viewer = User.objects.create_user("pelihat", password="x")
        StoreStaff.objects.create(store=self.store, user=viewer, role="viewer")
        client = Client()
        client.force_login(viewer)
        self.assertNotIn('class="mk-resi"', client.get("/books/orders/?tab=all&sumber=semua").content.decode())

    def test_adding_a_resi_from_the_card_saves_it(self):
        order = self.sale("MP3")
        self.client.post(f"/books/orders/{order.id}/tracking/", {"courier_code": "jne", "tracking_number": "CM123456789",
            "next": "/books/orders/?tab=all"})
        order.refresh_from_db()
        self.assertEqual((order.tracking_number, order.courier_code), ("CM123456789", "jne"))

    def test_the_page_shows_everything_by_default_and_the_website_view_says_so(self):
        self.sale("MP4")
        default = self.page().content.decode()
        self.assertNotIn("tidak ditampilkan", default)                # nothing hidden, no banner
        self.assertIn("MP4" if False else "Selesai", default)
        web = self.page("?sumber=web").content.decode()
        self.assertIn("1 pesanan marketplace tidak ditampilkan di sini", web)
        self.assertIn("Tampilkan semua", web)
        self.assertNotIn("sumber=web", web.split("Tampilkan semua")[0].split("href=")[-1])   # the link leaves the website view

    def test_a_partial_refresh_carries_the_tabs_too(self):
        self.sale("MP5")
        partial = self.client.get("/books/orders/?channel=shopee&tab=all", HTTP_HX_REQUEST="true").content.decode()
        self.assertIn("order-tabs", partial)                          # the tabs refresh with the list
        self.assertNotIn("<html", partial)

    def test_search_finds_an_order_by_its_resi(self):
        self.sale("MP6", resi="JY91008038038")
        self.sale("MP7", resi="CM05459265352")
        html = self.page("?sumber=semua&tab=all&q=JY91008038038").content.decode()
        self.assertIn("JY91008038038", html)
        self.assertNotIn("CM05459265352", html)

    def test_the_review_flag_is_visible_and_says_what_to_do(self):
        self.sale("MP8", sku="TIDAK-ADA")
        html = self.page("?tab=all").content.decode()
        self.assertIn("Perlu dicek", html)
        self.assertIn("Produk dibuat otomatis", html)                 # the reason is written out, not only a tooltip
        self.assertIn("Buka Pemetaan SKU", html)
        self.assertIn("/books/marketplace/sku/", html)

    def test_a_cancelled_order_without_shipping_data_has_no_empty_shipping_panel(self):
        self.sale("MP10", status="Dibatalkan")
        card = self.page("?tab=all").content.decode().split("<article")[1].split("</article>")[0]
        self.assertIn("Pesanan ini tidak menjadi penjualan", card)
        self.assertNotIn("mk-ship", card)

    def test_the_csv_has_the_courier_the_channel_and_the_marketplace_status(self):
        self.sale("MP9", courier="SiCepat REG", resi="004535364538")
        lines = self.client.get("/books/orders.csv").content.decode().splitlines()
        header, row = lines[0].split(","), lines[1]
        self.assertEqual(header[10:], ["tracking", "channel", "marketplace_order_id", "marketplace_status", "courier"])
        for expected in ("004535364538", "shopee", "MP9", "Selesai", "SiCepat REG"):
            self.assertIn(expected, row)

    def test_the_detail_page_shows_courier_and_resi_with_a_copy_button(self):
        order = self.sale("MP11", courier="JNE YES", resi="JY91008038038")
        html = self.client.get(f"/books/orders/{order.id}/").content.decode()
        for expected in ("Ekspedisi:", "JNE YES", "JY91008038038", 'data-copy="JY91008038038"', "Salin"):
            self.assertIn(expected, html)
        self.assertNotIn('name="courier_code"', html)                     # a resi is already there: no form
        self.assertNotIn(">0812<", html)                                  # a marketplace "phone" is only a username

    def test_the_detail_page_saves_courier_and_resi_together_and_returns_to_the_order(self):
        order = self.sale("MP12")
        html = self.client.get(f"/books/orders/{order.id}/").content.decode()
        self.assertIn('name="courier_code"', html)
        self.assertIn('<option value="sicepat">SiCepat</option>', html)
        reply = self.client.post(f"/books/orders/{order.id}/tracking/", {"courier_code": "sicepat",
            "tracking_number": "004685080641", "next": f"/books/orders/{order.id}/"})
        self.assertEqual(reply["Location"], f"/books/orders/{order.id}/")
        order.refresh_from_db()
        self.assertEqual((order.courier_code, order.tracking_number), ("sicepat", "004685080641"))

    def test_the_detail_page_shortens_a_masked_address(self):
        order = self.sale("MP13")
        Order.objects.filter(pk=order.pk).update(shipping_address="**********************************, Jakarta")
        html = self.client.get(f"/books/orders/{order.id}/").content.decode()
        self.assertIn("•••, Jakarta", html)
        self.assertNotIn("*****", html)

