"""The stock movement ledger is append-only, and every level's stock can be replayed from it."""
from decimal import Decimal
from itertools import pairwise

from django.db import connection
from test_sales_docs import Base

from apps.catalog.models import InventoryLevel
from apps.orders.models import InventoryMovement, MovementImmutable
from apps.orders.stock import receive_goods


class MovementLedgerTests(Base):
    def setUp(self):
        super().setUp()
        receive_goods(store=self.store, product=self.kabel, quantity=5, unit_cost=Decimal(60_000))
        self.invoice(kabel=2)
        self.movement = InventoryMovement.objects.filter(kind="receipt").first()

    def test_a_posted_movement_cannot_be_edited_or_deleted(self):
        self.movement.reason = "diubah"
        with self.assertRaises(MovementImmutable):
            self.movement.save()
        with self.assertRaises(MovementImmutable):
            self.movement.delete()
        with self.assertRaises(MovementImmutable):
            InventoryMovement.objects.filter(pk=self.movement.pk).update(quantity=1)
        with self.assertRaises(MovementImmutable):
            InventoryMovement.objects.all().delete()

    def test_database_trigger_refuses_raw_edits_on_postgres(self):
        if connection.vendor != "postgresql":
            self.skipTest("trigger is PostgreSQL only")
        from django.db import DatabaseError, transaction
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("UPDATE orders_inventorymovement SET quantity = 999 WHERE id = %s", [self.movement.pk])
        with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("DELETE FROM orders_inventorymovement WHERE id = %s", [self.movement.pk])

    def test_deleting_the_actor_is_still_allowed(self):
        from django.contrib.auth.models import User
        user = User.objects.create_user("gudang", password="x")
        InventoryMovement.objects.filter(pk=self.movement.pk)  # queryset stays usable
        row = InventoryMovement.objects.get(pk=self.movement.pk)
        InventoryMovement.objects.bulk_update([], ["actor"])  # no-op, bulk path untouched
        self.assertIsNone(row.actor)
        user.delete()

    def test_each_level_replays_from_its_movements(self):
        for level in InventoryLevel.objects.filter(warehouse__store=self.store):
            moves = list(InventoryMovement.objects.filter(inventory_level=level).order_by("id"))
            if not moves:
                continue
            for earlier, later in pairwise(moves):
                self.assertEqual(earlier.new_on_hand, later.previous_on_hand)
                self.assertEqual(earlier.new_reserved, later.previous_reserved)
            self.assertEqual(moves[-1].new_on_hand, level.on_hand)
            self.assertEqual(moves[-1].new_reserved, level.reserved)
