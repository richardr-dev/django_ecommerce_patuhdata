"""The partner edits a company after install: names, contact, tax identity and the trial end."""
import datetime

from django.db import transaction
from django.utils import timezone

from apps.tax.models import TaxProfile
from apps.tax.services import normalize_npwp

from .models import AuditLog
from .trial import set_trial_end


class CompanyError(Exception):
    """A refusal the page shows as a message."""


@transaction.atomic
def update_company(store, *, name, support_email="", support_phone="", legal_name="", npwp="", address="",
        taxpayer_type="company", is_pkp=False, trial_end="", actor=None):
    """Save the company's details. ``trial_end`` is a date (YYYY-MM-DD, ends that evening) or "" for full access."""
    name = (name or "").strip()
    if not name:
        raise CompanyError("Tulis nama perusahaan.")
    try:
        npwp = normalize_npwp(npwp)
    except ValueError as problem:
        raise CompanyError(str(problem)) from None
    if is_pkp and not npwp:
        raise CompanyError("PKP wajib punya NPWP.")
    if taxpayer_type not in dict(TaxProfile.TAXPAYER_TYPES):
        raise CompanyError("Jenis wajib pajak tidak dikenal.")
    end = None
    if trial_end:
        try:
            day = datetime.date.fromisoformat(trial_end)
        except ValueError:
            raise CompanyError("Tanggal akhir uji coba tidak valid.") from None
        end = timezone.make_aware(datetime.datetime.combine(day, datetime.time(23, 59, 59)))
    store.name = name[:160]
    store.support_email = (support_email or "").strip()
    store.support_phone = (support_phone or "").strip()[:30]
    store.save(update_fields=["name", "support_email", "support_phone"])
    profile = TaxProfile.objects.filter(store=store).first() or TaxProfile(store=store)
    profile.legal_name, profile.npwp, profile.address = (legal_name or "").strip(), npwp, (address or "").strip()
    profile.taxpayer_type, profile.is_pkp = taxpayer_type, bool(is_pkp)
    profile.save()
    if end != store.trial_ends_at and (end is None or store.trial_ends_at is None
            or end.date() != store.trial_ends_at.astimezone(timezone.get_current_timezone()).date()):
        set_trial_end(store, end, actor)
    AuditLog.objects.create(store=store, actor=actor, action="store.edited_by_partner", object_type="Store",
        object_id=str(store.pk), details={"name": store.name})
    return store
