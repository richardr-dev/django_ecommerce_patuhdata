from unittest.mock import MagicMock, patch

import requests
from django.test import SimpleTestCase

from apps.integrations.http import request_json


class ProviderHttpClientTests(SimpleTestCase):
    @patch("apps.integrations.http.requests.request")
    def test_sends_explicit_application_user_agent(self, request):
        response = MagicMock(ok=True)
        response.json.return_value = {"success": True}
        request.return_value = response
        request_json("https://api.example.test/resource")
        headers = request.call_args.kwargs["headers"]
        self.assertEqual(headers["User-Agent"], "NiagaKit-Commerce/0.1")
        self.assertNotIn("Python-urllib", headers["User-Agent"])

    @patch("apps.integrations.http.requests.request")
    def test_retries_one_transient_network_failure(self, request):
        response = MagicMock(ok=True)
        response.json.return_value = {"success": True}
        request.side_effect = [requests.ConnectionError("temporary"), response]
        self.assertTrue(request_json("https://api.example.test/resource")["success"])
        self.assertEqual(request.call_count, 2)
