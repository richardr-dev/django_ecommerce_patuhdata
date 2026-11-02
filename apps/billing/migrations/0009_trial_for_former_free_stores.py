from datetime import timedelta

from django.conf import settings
from django.db import migrations
from django.utils import timezone


def start_trials(apps, schema_editor):
    """Stores that joined while the default plan was free have no period end; once that plan
    became paid they read as lapsed. Give them the same trial a new store gets."""
    Subscription = apps.get_model("billing", "Subscription")
    Subscription.objects.filter(plan__price_monthly__gt=0, current_period_end__isnull=True).update(
        current_period_end=timezone.now() + timedelta(days=settings.TRIAL_DAYS))


class Migration(migrations.Migration):
    dependencies = [("billing", "0008_outlet_pricing")]
    operations = [migrations.RunPython(start_trials, migrations.RunPython.noop)]
