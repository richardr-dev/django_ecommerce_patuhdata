"""The back office moved from /merchant/ to /books/; old bookmarks, invite emails and open forms keep working."""
from django.test import TestCase


class LegacyMerchantUrlTests(TestCase):
    def test_a_get_goes_to_the_same_page_under_books_with_its_query(self):
        reply = self.client.get("/merchant/accounting/journal/?month=2026-11&q=kas")
        self.assertEqual((reply.status_code, reply["Location"]), (301, "/books/accounting/journal/?month=2026-11&q=kas"))

    def test_the_old_root_and_the_old_invite_link(self):
        self.assertEqual(self.client.get("/merchant/")["Location"], "/books/")
        self.assertEqual(self.client.get("/merchant/invite/Mw/abc-def/")["Location"], "/books/invite/Mw/abc-def/")

    def test_the_old_sign_in_page_goes_to_login(self):
        reply = self.client.get("/merchant/login/?next=/books/")
        self.assertEqual((reply.status_code, reply["Location"]), (301, "/login/?next=/books/"))

    def test_a_post_to_an_old_address_keeps_its_method(self):
        reply = self.client.post("/merchant/logout/", {"x": "1"})
        self.assertEqual((reply.status_code, reply["Location"]), (308, "/books/logout/"))

    def test_the_new_address_is_served_and_the_redirect_never_leaves_the_site(self):
        self.assertEqual(self.client.get("/books/").status_code, 302)                  # signed out: to the login
        reply = self.client.get("/merchant//evil.example/x")
        self.assertTrue(reply["Location"].startswith("/books/"))
        self.assertFalse(reply["Location"].startswith("//"))
