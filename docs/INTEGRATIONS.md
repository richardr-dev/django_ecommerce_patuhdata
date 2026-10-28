# Integration contract

DOKU Checkout is the primary payment gateway. Xendit remains available as a secondary adapter
(`PAYMENT_PROVIDER=xendit`).

Payments, accounting, and shipping are provider adapters rather than checkout-specific code.

For each production payment provider implement: `create_payment(order)`, signed `parse_webhook`,
`fetch_status(reference)`, `expire(reference)`, and `refund(order, amount)`. Webhook handling must
store a unique provider event before changing an order, acknowledge duplicates, compare amount and
currency, and never trust browser redirects as proof of payment.

Accounting adapters consume durable outbox topics such as `order.paid`, `order.refunded`, and
`payout.reconciled`. They return an external document ID and idempotency key. Begin with normalized
CSV exports, then build adapters for the systems clients actually pay for (for example Accurate,
Jurnal/Mekari, or custom ERP). Mapping needs an explicit chart of accounts, tax codes, SKU/customer
policy, discounts, shipping, gateway fees, refunds, and payout reconciliation.

Shipping adapters quote services using destination IDs and weight/dimensions, create labels, and
normalize tracking events. Do not embed a courier's response schema in orders.

## Current durable events

- `order.created`: customer confirmation and merchant notification.
- `payment.received`: paid receipt and fulfillment trigger.
- `order.status_changed`: processing, shipment, cancellation, and completion updates.

Workers claim outbox rows transactionally, increment `attempts`, and set `processed_at` only after
provider acknowledgement. Keep promotional consent separate from transactional notifications.

## DOKU Checkout

Set `PAYMENT_PROVIDER=doku`, `DOKU_CLIENT_ID`, and `DOKU_SECRET_KEY`. A separately issued
`DOKU_API_KEY` may be stored for other DOKU services but is not used for Checkout HMAC signing.
Sandbox is the default;
set `DOKU_API_URL=https://api.doku.com` only after certification. Checkout signs the exact JSON
request body and redirects customers to the hosted DOKU Checkout URL. Configure this public HTTPS
notification URL for every enabled DOKU payment channel:

```text
https://YOUR_DOMAIN/integrations/webhooks/doku/
```

`PUBLIC_BASE_URL=http://localhost:8000` is suitable only for local browser testing. DOKU cannot
deliver notifications to localhost, an authenticated URL, a VPN-only host, or an unusual port.
For sandbox notification testing, expose the application through a stable public HTTPS domain
(DOKU currently prohibits ngrok domains), set that domain as `PUBLIC_BASE_URL`, and configure the
URL above in DOKU Back Office for every enabled payment channel.

Notifications are signature-verified, deduplicated by DOKU request ID, and checked against the
order amount and currency before fulfilment. For DOKU Checkout, `FAILED` events are acknowledged
without cancelling the order so the customer can retry with another channel.

### Payment session lifecycle

1. Checkout reserves stock, the coupon and loyalty points in one transaction and commits it. The
   order already carries its DOKU invoice number (`NK` + order ID) and a 15-minute creation window.
2. Only then is DOKU called. If the call fails, the order is cancelled, stock/coupon/points are
   released, the cart is kept, and the customer can retry with a fresh idempotency key.
3. On success the order stores the checkout URL and expires `DOKU_PAYMENT_DUE_MINUTES + 5` minutes
   later.
4. When an order expires, the worker first calls `GET /orders/v1/status/{invoice}` (signed without
   a Digest component). `SUCCESS` marks the order paid with the same amount checks as the
   notification; `PENDING` keeps the reservation; `FAILED`, `EXPIRED` or HTTP 404 (no channel
   chosen) cancels it. If DOKU is unreachable the worker retries each cycle and cancels after six
   hours; a payment that still arrives later is kept as an unreconciled transaction.

### Refunds

DOKU refunds are channel-specific and most QRIS/virtual-account payments cannot be refunded by API.
Finance refunds these by bank transfer and records them in the order screen with **Transfer manual**
and the transfer reference. The refund is stored with `method=manual_transfer`, the reference in its
payload, and the same over-refund protection as gateway refunds.

### Sandbox certification checklist

- Notification for each enabled channel (QRIS, VA per bank, e-wallets) with correct signature.
- Duplicate notification is acknowledged once; amount mismatch is rejected and listed under
  reconciliation.
- Status API returns `SUCCESS`/`PENDING`/`EXPIRED` for an invoice and 404 before a channel is chosen;
  confirm the response shape matches `DokuPaymentProvider.fetch_status`.
- Expiry while unpaid releases stock; expiry after a missed notification marks the order paid.
- Manual refund recorded and exported for finance.

The handler compares `x-callback-token`, reference, session ID, amount, and IDR currency; stores a
deduplicated `PaymentEvent`; and treats the browser return URL as display-only. Completed sessions
mark orders paid. Expired sessions cancel the order and release reserved inventory.

## Xendit Payment Sessions

Set `PAYMENT_PROVIDER=xendit`, `XENDIT_SECRET_KEY`, and `XENDIT_WEBHOOK_VERIFICATION_TOKEN`. Checkout
creates a server-side `PAY` session and redirects to Xendit Hosted Checkout. When `PUBLIC_BASE_URL`
uses HTTPS, it is included as the success/cancel return URL. On localhost HTTP those optional URLs
are omitted, so Xendit shows its default result page. Configure this callback in the dashboard:

```text
https://YOUR_DOMAIN/integrations/webhooks/xendit/
```

## Biteship checkout rates

Set `BITESHIP_API_KEY`, `BITESHIP_ORIGIN_POSTAL_CODE`, and optionally `BITESHIP_COURIERS`. Checkout
calls Biteship with the origin and customer postal codes plus product weight and dimensions. The
returned quote is bound to the server session, destination postal code, and current cart. Changing
the postal code or cart requires a fresh quote.

For sandbox/demo usage, use a `biteship_test.` token and keep `BITESHIP_ALLOW_LIVE=0`. Production
requires a rotated live token and `BITESHIP_ALLOW_LIVE=1`.

## Biteship shipment booking

Checkout stores the Biteship courier (`courier_code`) and service (`courier_service_code`) that the
customer paid for. When an order is **Packed**, the merchant order screen shows **Pesan kurir**,
which calls `POST /v1/orders` with that courier, the order lines, and the pickup contact:

- `BITESHIP_ORIGIN_CONTACT_NAME`, `BITESHIP_ORIGIN_CONTACT_PHONE`, `BITESHIP_ORIGIN_ADDRESS`, with
  fallbacks to the store name, store support phone and the highest-priority warehouse address.
- `BITESHIP_ORIGIN_POSTAL_CODE` (also used for rates).

The order is claimed before the request so it can only be booked once. Biteship has no idempotency
key, so the request is never retried automatically: an HTTP 4xx rejection releases the claim for a
retry, while a timeout leaves the order marked "status booking tidak pasti" until someone checks the
Biteship dashboard (and enters the waybill manually if it was booked). The Biteship order ID is
stored in `external_shipping_id` and the waybill in `tracking_number`; couriers that assign the
waybill after pickup fill it in on the next sync.

The worker syncs booked shipments with `GET /v1/orders/{id}` every 15 minutes. Biteship statuses map
to tracking states (`picked` → in transit, `dropping_off` → out for delivery, `delivered`,
`returned`, and `rejected`/`courier_not_found`/`on_hold`/`cancelled` → exception). In transit moves
a packed order to shipped; delivered moves it to delivered. Orders without a booking keep using
the manual waybill + RajaOngkir tracking flow.

Certify in the Biteship sandbox: booking per enabled courier/service, the `courier.history` or
`history` shape of the order response, waybill assignment timing, and cancellation handling.

## RajaOngkir checkout rates

Set `SHIPPING_RATE_PROVIDER=rajaongkir`, `RAJAONGKIR_API_KEY`, and either
`RAJAONGKIR_ORIGIN_ID` or `RAJAONGKIR_ORIGIN_POSTAL_CODE`. Set `RAJAONGKIR_COURIERS` to the enabled
couriers. Checkout searches `/destination/domestic-destination` and submits a destination ID plus
total cart weight to `/calculate/domestic-cost`. Customers may select an address result or enter an
exact five-digit postcode; the server resolves exact origin/destination postcodes to RajaOngkir IDs
and caches the normalized result for one day. Ambiguous postcodes require selecting a district result.
The resolved destination, postal code, cart signature, service, ETA, and price are bound to the server
session and persisted on the order.

These are live RajaOngkir endpoints. When RajaOngkir is selected, provider errors are shown to the
checkout and the application does not silently substitute manually configured/demo prices.

## Production activation

1. Keep provider keys in a secret manager.
2. Complete the DOKU sandbox certification checklist above (Xendit only if it is enabled).
3. Verify Biteship quotes for each supported courier and destination before enabling live access.
4. Connect outbox processing to WhatsApp Business and email with retries and delivery logs.
5. Certify sandbox payments, refunds, expiry, reconciliation, and restores before launch.
