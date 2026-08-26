import hmac

from django.http import Http404
from django.shortcuts import get_object_or_404

from .models import Order


def remember_customer_order(request, order):
    """Bind a newly-created order to the browser session without exposing customer data."""
    order_ids = list(request.session.get("customer_order_ids", []))[-19:]
    value = str(order.id)
    if value not in order_ids:
        order_ids.append(value)
    request.session["customer_order_ids"] = order_ids


def get_customer_order_or_404(request, order_id, *, queryset=None):
    queryset = queryset if queryset is not None else Order.objects.all()
    order = get_object_or_404(queryset, id=order_id, store=request.store)
    session_authorized = str(order.id) in request.session.get("customer_order_ids", [])
    account_authorized = request.user.is_authenticated and order.customer_user_id == request.user.id
    supplied_token = request.GET.get("token") or request.POST.get("token") or ""
    token_authorized = supplied_token and hmac.compare_digest(supplied_token, str(order.access_token))
    if not session_authorized and not token_authorized and not account_authorized:
        raise Http404
    return order
