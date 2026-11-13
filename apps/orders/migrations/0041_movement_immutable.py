from django.db import migrations

# On PostgreSQL the database itself refuses to change or delete a stock movement, so no script or
# console session can rewrite history. Only deleting a user may blank ``actor_id`` (SET_NULL).
# TRUNCATE (used to reset test databases) does not fire row triggers.
CREATE = """
CREATE OR REPLACE FUNCTION orders_movement_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'inventory movements are append-only (delete refused)';
    END IF;
    IF (to_jsonb(OLD) - 'actor_id') IS DISTINCT FROM (to_jsonb(NEW) - 'actor_id') THEN
        RAISE EXCEPTION 'inventory movements are append-only (update refused)';
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS orders_movement_guard ON orders_inventorymovement;
CREATE TRIGGER orders_movement_guard BEFORE UPDATE OR DELETE ON orders_inventorymovement
    FOR EACH ROW EXECUTE FUNCTION orders_movement_guard();
"""
DROP = """
DROP TRIGGER IF EXISTS orders_movement_guard ON orders_inventorymovement;
DROP FUNCTION IF EXISTS orders_movement_guard();
"""


def add(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(CREATE, params=None)


def remove(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(DROP, params=None)


class Migration(migrations.Migration):
    dependencies = [("orders", "0040_return_quantity")]
    operations = [migrations.RunPython(add, remove)]
