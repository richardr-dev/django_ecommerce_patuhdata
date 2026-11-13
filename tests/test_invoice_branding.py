"""A store's own logo, colour and footer on everything it sends to a buyer."""
import io
import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.template.loader import render_to_string
from django.test import override_settings
from django.urls import reverse
from PIL import Image
from test_sales_docs import Base

from apps.tax import branding, sharing
from apps.tax.models import TaxProfile

MEDIA = tempfile.mkdtemp()


def png(color=(200, 30, 30), size=(60, 30)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return SimpleUploadedFile("logo.png", buffer.getvalue(), content_type="image/png")


@override_settings(MEDIA_ROOT=MEDIA)
class BrandingTests(Base):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)
        self.url = reverse("merchant-invoice-style")
        self.inv = self.invoice(kabel=1, credit=True)

    def save_style(self, **extra):
        data = {"invoice_show_name": "on", "invoice_accent": "#be123c",
            "invoice_footer": "Transfer ke BCA 123-456-7890. Terima kasih!", **extra}
        return self.client.post(self.url, data)

    def test_upload_a_logo_and_see_it_on_every_document(self):
        response = self.save_style(invoice_logo=png())
        self.assertRedirects(response, self.url)
        profile = TaxProfile.objects.get(store=self.store)
        self.assertTrue(profile.invoice_logo)
        logo_url = profile.invoice_logo.url
        pages = {
            "detail": reverse("merchant-invoice-detail", args=[self.inv.pk]),
            "surat jalan": reverse("merchant-delivery-note", args=[self.inv.pk]),
            "buyer link": reverse("invoice-public", args=[sharing.ensure_token(self.inv)]),
            "tagihan": reverse("merchant-receivables") + f"?customer={self.customer.pk}",
        }
        self.inv.order.customer = self.customer
        self.inv.order.save()
        for label, url in pages.items():
            html = self.client.get(url).content.decode()
            self.assertIn(logo_url, html, label)
            self.assertIn("#be123c", html, label)
        self.assertIn("Transfer ke BCA 123-456-7890", self.client.get(pages["buyer link"]).content.decode())

    def test_pdf_embeds_the_logo_because_it_cannot_fetch_files(self):
        self.save_style(invoice_logo=png())
        html = render_to_string("merchant/invoice_pdf.html", {"invoices": [self.inv]})
        self.assertIn("data:image/png;base64,", html)
        self.assertIn("#be123c", html)
        self.assertIn("Transfer ke BCA", html)

    def test_print_page_uses_the_logo_too(self):
        self.save_style(invoice_logo=png())
        html = render_to_string("tax/invoice.html", {"invoice": self.inv})
        self.assertIn(TaxProfile.objects.get(store=self.store).invoice_logo.url, html)

    def test_falls_back_to_the_shop_logo_then_to_the_initial(self):
        b = branding.brand(self.store, self.inv)
        self.assertFalse(b.logo_url)
        self.assertEqual(b.initial, self.inv.seller_name[:1].upper())
        self.store.logo.save("shop.png", png(), save=True)
        self.assertEqual(branding.brand(self.store, self.inv).logo_url, self.store.logo.url)
        self.save_style(invoice_logo=png((0, 0, 200)))
        own = TaxProfile.objects.get(store=self.store).invoice_logo.url
        fresh = type(self.store).objects.get(pk=self.store.pk)
        self.assertEqual(branding.brand(fresh, self.inv).logo_url, own)

    def test_a_file_that_is_not_an_image_is_refused(self):
        fake = SimpleUploadedFile("logo.png", b"<script>alert(1)</script>", content_type="image/png")
        response = self.save_style(invoice_logo=fake)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "bukan gambar yang valid")
        self.assertFalse(TaxProfile.objects.get(store=self.store).invoice_logo)

    def test_wrong_extension_and_huge_images_are_refused(self):
        svg = SimpleUploadedFile("logo.svg", b"<svg/>", content_type="image/svg+xml")
        self.assertEqual(self.save_style(invoice_logo=svg).status_code, 200)
        self.assertFalse(TaxProfile.objects.get(store=self.store).invoice_logo)
        big = png(size=(branding.MAX_LOGO_PIXELS + 1, 10))
        self.assertContains(self.save_style(invoice_logo=big), "terlalu besar")

    def test_bad_colour_is_refused_and_logo_can_be_removed(self):
        self.save_style(invoice_logo=png())
        self.assertEqual(self.save_style(invoice_accent="red").status_code, 200)
        self.assertEqual(TaxProfile.objects.get(store=self.store).invoice_accent, "#be123c")
        self.save_style(**{"invoice_logo-clear": "on"})
        self.assertFalse(TaxProfile.objects.get(store=self.store).invoice_logo)

    def test_name_can_be_hidden_when_the_logo_has_it(self):
        self.save_style(invoice_logo=png(), invoice_show_name="")
        html = self.client.get(reverse("merchant-delivery-note", args=[self.inv.pk])).content.decode()
        self.assertNotIn(f"<b style=\"display:block\">{self.inv.seller_name}", html)

    def test_only_finance_roles_can_change_it_and_the_menu_links_to_it(self):
        self.assertContains(self.client.get(reverse("merchant-dashboard")), self.url)
        from django.contrib.auth.models import User

        from apps.shops.models import StoreStaff
        viewer = User.objects.create_user("vw", password="x")
        StoreStaff.objects.create(store=self.store, user=viewer, role="viewer")
        self.client.force_login(viewer)
        self.assertIn(self.client.get(self.url).status_code, (403, 404))
