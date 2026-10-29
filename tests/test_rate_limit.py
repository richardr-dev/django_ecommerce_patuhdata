from django.contrib.auth.models import User
from django.test import Client, RequestFactory, TestCase, override_settings

from apps.shops.models import Store
from config.ratelimit import client_ip


class RateLimitTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user("limit-owner")
        Store.objects.create(owner=owner, name="Limit", slug="limit")

    def login(self, username="ayu@example.com", ip="10.0.0.1"):
        return Client(REMOTE_ADDR=ip).post("/account/login/",
            {"username": username, "password": "wrong"})

    def test_login_is_limited_per_ip_with_retry_after(self):
        for n in range(10):
            self.assertEqual(self.login(username=f"user{n}@example.com").status_code, 200)
        response = self.login(username="another@example.com")
        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "Terlalu banyak percobaan", status_code=429)
        self.assertGreater(int(response["Retry-After"]), 0)
        self.assertEqual(self.login(ip="10.0.0.2").status_code, 200)

    def test_one_username_is_limited_across_many_ips(self):
        for n in range(10):
            self.assertEqual(self.login(ip=f"10.0.1.{n}").status_code, 200)
        self.assertEqual(self.login(ip="10.0.1.99").status_code, 429)

    def test_get_requests_are_never_limited(self):
        client = Client(REMOTE_ADDR="10.0.2.1")
        for _ in range(15):
            self.assertEqual(client.get("/account/login/").status_code, 200)

    def test_merchant_and_platform_admin_logins_are_limited(self):
        for path in ("/merchant/login/", "/platform-admin/login/"):
            client = Client(REMOTE_ADDR=f"10.0.3.{len(path)}")
            statuses = [client.post(path, {"username": f"u{n}", "password": "x"}).status_code
                for n in range(11)]
            self.assertEqual(statuses[-1], 429, path)

    def test_newsletter_and_api_token_limits(self):
        client = Client(REMOTE_ADDR="10.0.4.1")
        for _ in range(10):
            client.post("/newsletter/subscribe/", {"email": "a@example.com", "consent": "on"})
        self.assertEqual(client.post("/newsletter/subscribe/", {"email": "a@example.com"}).status_code, 429)
        api = Client(REMOTE_ADDR="10.0.4.2")
        for _ in range(10):
            api.post("/api/v1/auth/token/", {"email": "a@example.com", "password": "x"},
                content_type="application/json")
        response = api.post("/api/v1/auth/token/", {}, content_type="application/json")
        self.assertEqual(response.status_code, 429)
        self.assertIn("retry_after", response.json())

    @override_settings(RATE_LIMIT_ENABLED=False)
    def test_limits_can_be_disabled(self):
        for _ in range(12):
            self.assertEqual(self.login().status_code, 200)


class ClientIpTests(TestCase):
    def request(self, forwarded):
        return RequestFactory().get("/", REMOTE_ADDR="172.18.0.5", HTTP_X_FORWARDED_FOR=forwarded)

    def test_forwarded_header_ignored_without_trusted_proxy(self):
        self.assertEqual(client_ip(self.request("1.2.3.4")), "172.18.0.5")

    @override_settings(TRUSTED_PROXY_HOPS=1)
    def test_spoofed_leftmost_address_is_ignored_behind_one_proxy(self):
        # The client sent "6.6.6.6"; Caddy appended the real peer, 203.0.113.7.
        self.assertEqual(client_ip(self.request("6.6.6.6, 203.0.113.7")), "203.0.113.7")
        self.assertEqual(client_ip(self.request("")), "172.18.0.5")
