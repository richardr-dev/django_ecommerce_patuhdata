"""Global search of the back office: invoices, orders, customers, products, supplier bills and
journals matching what was typed. The topbar asks for it while typing; results are links."""
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import render
from django.urls import reverse

from apps.accounting.models import Bill, JournalEntry, Supplier
from apps.catalog.models import Product
from apps.shops.models import Customer
from apps.tax.models import CreditNote, Invoice, Quote

from .models import Order
from .views import _merchant_context, _staff_store

LIMIT = 5


def _group(title, rows):
    return {"title": title, "rows": rows} if rows else None


def search(store, query):
    """Groups of ``{"label", "hint", "url"}`` for ``query`` (at least two characters)."""
    if len(query) < 2:
        return []
    groups = []
    invoices = Invoice.objects.filter(store=store).filter(Q(number__icontains=query)
        | Q(buyer_name__icontains=query))[:LIMIT]
    groups.append(_group("Invoice", [{"label": i.number, "hint": f"{i.buyer_name} · Rp{i.total:,.0f}"
        .replace(",", "."), "url": reverse("merchant-invoice-detail", args=[i.id])} for i in invoices]))
    notes = CreditNote.objects.filter(store=store).filter(Q(number__icontains=query)
        | Q(buyer_name__icontains=query))[:LIMIT]
    groups.append(_group("Nota kredit", [{"label": n.number, "hint": n.buyer_name,
        "url": reverse("merchant-credit-note-detail", args=[n.id])} for n in notes]))
    quotes = Quote.objects.filter(store=store).filter(Q(number__icontains=query)
        | Q(buyer_name__icontains=query))[:LIMIT]
    groups.append(_group("Penawaran", [{"label": q.number, "hint": q.buyer_name,
        "url": reverse("merchant-quote-detail", args=[q.id])} for q in quotes]))
    orders = Order.objects.filter(store=store).filter(Q(invoice_number__icontains=query)
        | Q(customer_name__icontains=query) | Q(external_order_id__icontains=query)
        | Q(phone__icontains=query))[:LIMIT]
    groups.append(_group("Pesanan", [{"label": o.invoice_number or str(o.id)[:8],
        "hint": f"{o.customer_name} · {o.get_status_display()}",
        "url": reverse("merchant-order-detail", args=[o.id])} for o in orders]))
    customers = Customer.objects.filter(store=store).filter(Q(name__icontains=query)
        | Q(phone__icontains=query) | Q(email__icontains=query)
        | Q(company_name__icontains=query))[:LIMIT]
    groups.append(_group("Pelanggan", [{"label": c.name, "hint": c.company_name or c.phone,
        "url": reverse("merchant-customer-detail", args=[c.id])} for c in customers]))
    products = Product.objects.filter(store=store).filter(Q(name__icontains=query)
        | Q(sku__icontains=query))[:LIMIT]
    groups.append(_group("Produk", [{"label": p.name, "hint": p.sku,
        "url": reverse("merchant-entity-edit", args=["products", p.id])} for p in products]))
    bills = Bill.objects.filter(store=store).filter(Q(number__icontains=query)
        | Q(supplier__name__icontains=query)).select_related("supplier")[:LIMIT]
    groups.append(_group("Tagihan supplier", [{"label": b.number or "Tanpa nomor",
        "hint": b.supplier.name, "url": reverse("merchant-bill-detail", args=[b.id])}
        for b in bills]))
    suppliers = Supplier.objects.filter(store=store, name__icontains=query)[:LIMIT]
    groups.append(_group("Supplier", [{"label": s.name, "hint": s.phone,
        "url": reverse("merchant-supplier-detail", args=[s.id])} for s in suppliers]))
    journals = JournalEntry.objects.filter(store=store, memo__icontains=query)[:LIMIT]
    groups.append(_group("Jurnal", [{"label": f"JU-{j.id}", "hint": j.memo[:60],
        "url": reverse("merchant-journal-detail", args=[j.id])} for j in journals]))
    return [group for group in groups if group]


@login_required(login_url="/login/")
def merchant_search(request):
    store = _staff_store(request)
    query = request.GET.get("q", "").strip()[:80]
    return render(request, "merchant/_search_results.html", _merchant_context(request, store,
        groups=search(store, query), query=query))
