from django.db import migrations


def strip_uuid_dashes(apps, schema_editor):
    """Migration 0025 stored UUIDs with dashes; Django's SQLite backend queries without dashes.
    Convert all id values to 32-char no-dash hex so ORM pk-based lookups work."""
    db = schema_editor.connection
    if db.vendor != "sqlite":
        return  # PostgreSQL stores real uuids; only SQLite kept text ids
    with db.cursor() as cursor:
        cursor.execute("SELECT id FROM shops_customer")
        rows = cursor.fetchall()
    for (old_id,) in rows:
        old_str = str(old_id)
        if '-' not in old_str:
            continue  # already no-dash
        no_dash = old_str.replace('-', '')
        schema_editor.execute(
            f"UPDATE shops_customer SET id = '{no_dash}' WHERE id = '{old_str}'"
        )


class Migration(migrations.Migration):

    dependencies = [
        ('shops', '0025_customer_uuid_separate_field'),
    ]

    operations = [
        migrations.RunPython(strip_uuid_dashes, migrations.RunPython.noop),
    ]
