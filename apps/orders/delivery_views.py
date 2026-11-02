"""Back office for delivery scheduling: dates, closed days and time slots, and the production
recap that tells a made-to-order seller what to make for each delivery date."""
from datetime import date, timedelta

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Sum
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.shops.models import AuditLog

from .models import DeliverySettings, DeliverySlot, Order, OrderItem
from .scheduling import RELEASED_STATUSES
from .views import _merchant_context, _require_roles, _staff_store

SETTINGS_ROLES = {"admin", "manager"}
RECAP_ROLES = {"admin", "manager", "ops", "warehouse", "customer_service", "viewer"}
RECAP_DAYS = 14


class DeliverySettingsForm(forms.ModelForm):
    closed_weekdays = forms.MultipleChoiceField(choices=DeliverySettings.WEEKDAYS, required=False,
        widget=forms.CheckboxSelectMultiple, label="Hari libur (tidak ada pengiriman)")

    class Meta:
        model = DeliverySettings
        fields = ["enabled", "min_days", "max_days", "closed_weekdays"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial["closed_weekdays"] = sorted(str(day) for day in self.instance.closed_days)

    def clean_closed_weekdays(self):
        days = self.cleaned_data["closed_weekdays"]
        if len(days) == 7:
            raise forms.ValidationError("Sisakan minimal satu hari untuk pengiriman.")
        return ",".join(sorted(days))

    def clean(self):
        cleaned = super().clean()
        if (cleaned.get("min_days") or 0) > (cleaned.get("max_days") or 0):
            self.add_error("max_days", "Harus sama atau lebih besar dari batas pemesanan.")
        return cleaned


class DeliverySlotForm(forms.ModelForm):
    class Meta:
        model = DeliverySlot
        fields = ["start_time", "end_time", "capacity"]
        widgets = {"start_time": forms.TimeInput(attrs={"type": "time"}),
            "end_time": forms.TimeInput(attrs={"type": "time"})}

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("start_time"), cleaned.get("end_time")
        if start and end and end <= start:
            self.add_error("end_time", "Jam selesai harus setelah jam mulai.")
        return cleaned


@login_required(login_url="/merchant/login/")
def merchant_delivery(request):
    store = _staff_store(request)
    _require_roles(request, store, SETTINGS_ROLES | {"viewer"})
    settings_obj = DeliverySettings.objects.filter(store=store).first() or DeliverySettings(store=store)
    form = DeliverySettingsForm(None, instance=settings_obj)
    slot_form = DeliverySlotForm(None)
    if request.method == "POST":
        _require_roles(request, store, SETTINGS_ROLES)
        action = request.POST.get("action")
        if action == "settings":
            form = DeliverySettingsForm(request.POST, instance=settings_obj)
            if form.is_valid():
                form.instance.store = store
                form.save()
                AuditLog.objects.create(store=store, actor=request.user,
                    action="delivery.settings_updated", object_type="DeliverySettings",
                    object_id=str(form.instance.id), details={"enabled": form.instance.enabled})
                messages.success(request, "Jadwal pengiriman disimpan.")
                return redirect("merchant-delivery")
        elif action == "add_slot":
            slot_form = DeliverySlotForm(request.POST)
            if slot_form.is_valid():
                slot_form.instance.store = store
                slot_form.save()
                messages.success(request, f"Jam {slot_form.instance.label} ditambahkan.")
                return redirect("merchant-delivery")
        elif action in {"toggle_slot", "delete_slot"}:
            slot = get_object_or_404(DeliverySlot, id=request.POST.get("slot_id"), store=store)
            if action == "toggle_slot":
                slot.is_active = not slot.is_active
                slot.save(update_fields=["is_active"])
            elif slot.orders.exists():
                # Past orders keep pointing at it; hide it from checkout instead.
                slot.is_active = False
                slot.save(update_fields=["is_active"])
                messages.info(request, f"Jam {slot.label} sudah dipakai pesanan, jadi dinonaktifkan.")
            else:
                slot.delete()
            return redirect("merchant-delivery")
        else:
            raise Http404
    preorder_products = store.products.filter(is_preorder=True, is_active=True).order_by("name")
    return render(request, "merchant/delivery.html", _merchant_context(request, store, form=form,
        slot_form=slot_form, slots=store.delivery_slots.all(), preorder_products=preorder_products))


@login_required(login_url="/merchant/login/")
def merchant_production_recap(request):
    """What to make and deliver per date: product totals and orders per slot."""
    store = _staff_store(request)
    _require_roles(request, store, RECAP_ROLES)
    today = timezone.localdate()
    scheduled = Order.objects.filter(store=store, delivery_date__isnull=False).exclude(
        status__in=RELEASED_STATUSES | {"pending"})
    upcoming = dict(scheduled.filter(delivery_date__range=(today, today + timedelta(days=RECAP_DAYS)))
        .values_list("delivery_date").annotate(total=Count("id")))
    try:
        day = date.fromisoformat(request.GET.get("date", ""))
    except ValueError:
        day = min(upcoming) if upcoming else today
    orders = scheduled.filter(delivery_date=day).select_related("delivery_slot").prefetch_related(
        "items").order_by("delivery_slot__start_time", "created_at")
    totals = (OrderItem.objects.filter(order__in=orders).values("name", "variant_name", "sku")
        .annotate(quantity=Sum("quantity")).order_by("-quantity", "name"))
    slots = {}
    for order in orders:
        slots.setdefault(order.delivery_slot_label or "Tanpa jam", []).append(order)
    days = [today + timedelta(days=offset) for offset in range(RECAP_DAYS + 1)]
    return render(request, "merchant/production_recap.html", _merchant_context(request, store,
        day=day, days=[{"date": d, "count": upcoming.get(d, 0)} for d in days], totals=totals,
        slots=slots, order_count=orders.count(),
        unpaid=Order.objects.filter(store=store, delivery_date=day, status="pending").count()))
