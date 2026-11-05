"""Log aktivitas: owners and store admins see who changed what; others cannot."""
from django.contrib.auth.models import User
from django.test import Client, TestCase

from apps.shops.models import AuditLog, Store, StoreStaff


class ActivityTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner", first_name="Sri")
        self.store = Store.objects.create(owner=self.owner, name="Ruuma", slug="ruuma")
        self.clerk = User.objects.create_user("clerk", first_name="Budi")
        StoreStaff.objects.create(store=self.store, user=self.clerk, role="warehouse")
        AuditLog.objects.create(store=self.store, actor=self.clerk, action="inventory.adjusted",
            object_type="InventoryLevel", object_id="7", details={"label": "Kopi"})
        AuditLog.objects.create(store=self.store, actor=self.owner, action="staff.invited",
            object_type="User", object_id="2", details={"role": "warehouse"})
        other = Store.objects.create(owner=User.objects.create_user("x"), name="Lain", slug="lain")
        AuditLog.objects.create(store=other, action="order.shipped", object_type="Order", object_id="1")

    def test_owner_sees_the_log_with_filters(self):
        client = Client()
        client.force_login(self.owner)
        page = client.get("/merchant/activity/")
        self.assertContains(page, "menyesuaikan")
        self.assertContains(page, "Budi")
        self.assertContains(page, "mengundang")
        self.assertNotContains(page, "mengirim")  # another store's log
        filtered = client.get("/merchant/activity/?group=Stok")
        self.assertContains(filtered, "menyesuaikan")
        self.assertNotContains(filtered, "mengundang")
        by_person = client.get(f"/merchant/activity/?actor={self.owner.id}")
        self.assertNotContains(by_person, "menyesuaikan")

    def test_staff_without_admin_role_cannot_see_it(self):
        client = Client()
        client.force_login(self.clerk)
        self.assertEqual(client.get("/merchant/activity/").status_code, 404)
