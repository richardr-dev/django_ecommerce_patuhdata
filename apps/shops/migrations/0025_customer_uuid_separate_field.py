import uuid
from django.db import migrations, models


def fix_integer_ids(apps, schema_editor):
    """Migration 0024 changed id to UUIDField but SQLite kept integer values.
    Replace each integer id with a proper UUID string using raw SQL."""
    db = schema_editor.connection
    if db.vendor != "sqlite":
        return  # PostgreSQL stores real uuids; only SQLite kept text ids
    with db.cursor() as cursor:
        cursor.execute("SELECT id FROM shops_customer")
        rows = cursor.fetchall()
    for (old_id,) in rows:
        # Skip rows that already look like UUIDs (36-char hex string with dashes)
        old_str = str(old_id)
        if len(old_str) == 36 and old_str.count('-') == 4:
            continue
        new_uuid = str(uuid.uuid4())
        schema_editor.execute(
            f"UPDATE shops_customer SET id = '{new_uuid}' WHERE id = '{old_str}'"
        )


class Migration(migrations.Migration):

    dependencies = [
        ('shops', '0024_customer_uuid_code'),
    ]

    operations = [
        # Fix corrupted integer id values left by migration 0024
        migrations.RunPython(fix_integer_ids, migrations.RunPython.noop),
    ]
