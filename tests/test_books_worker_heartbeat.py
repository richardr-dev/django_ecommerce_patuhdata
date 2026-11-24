"""The books worker leaves a heartbeat, which its container health check reads."""
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from apps.integrations.models import WorkerHeartbeat


class BooksHeartbeatTests(TestCase):
    def test_run_books_records_a_heartbeat(self):
        self.assertFalse(WorkerHeartbeat.objects.filter(name="run_books").exists())
        call_command("run_books", stdout=StringIO())
        self.assertTrue(WorkerHeartbeat.objects.filter(name="run_books").exists())
