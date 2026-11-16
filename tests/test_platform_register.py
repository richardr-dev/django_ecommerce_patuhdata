"""On the platform (back office) host, accounts are set up by sales: no self sign-up."""
from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings


@override_settings(PLATFORM_DOMAIN="patuhdata.test", ALLOWED_HOSTS=["*"],
    PLATFORM_COMPANY_URL="https://patuhdata.id")
class PlatformRegisterTests(TestCase):
    def setUp(self):
        self.client = Client(HTTP_HOST="patuhdata.test")

    def test_register_points_to_sales_instead_of_failing(self):
        response = self.client.get("/account/register/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'href="https://patuhdata.id/contact"')
        self.assertContains(response, 'href="/login/"')
        self.assertNotContains(response, "<form")

    def test_signed_in_user_goes_to_the_back_office(self):
        self.client.force_login(User.objects.create_user("owner"))
        response = self.client.get("/account/register/")
        self.assertRedirects(response, "/books/", fetch_redirect_response=False)

    def test_login_links_to_sales_sign_up(self):
        response = self.client.get("/login/")
        self.assertContains(response, "Hubungi tim sales")
        self.assertContains(response, "logo-accounting.png")
