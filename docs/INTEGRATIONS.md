# Integration contract

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

## Xendit Payment Sessions

Set `PAYMENT_PROVIDER=xendit`, `XENDIT_SECRET_KEY`, and `XENDIT_WEBHOOK_VERIFICATION_TOKEN`. Checkout
creates a server-side `PAY` session and redirects to Xendit Hosted Checkout. When `PUBLIC_BASE_URL`
uses HTTPS, it is included as the success/cancel return URL. On localhost HTTP those optional URLs
are omitted, so Xendit shows its default result page. Configure this callback in the dashboard:

```text
https://YOUR_DOMAIN/integrations/webhooks/xendit/
```

The handler compares `x-callback-token`, reference, session ID, amount, and IDR currency; stores a
deduplicated `PaymentEvent`; and treats the browser return URL as display-only. Completed sessions
mark orders paid. Expired sessions cancel the order and release reserved inventory.

## Biteship checkout rates

Set `BITESHIP_API_KEY`, `BITESHIP_ORIGIN_POSTAL_CODE`, and optionally `BITESHIP_COURIERS`. Checkout
calls Biteship with the origin and customer postal codes plus product weight and dimensions. The
returned quote is bound to the server session, destination postal code, and current cart. Changing
the postal code or cart requires a fresh quote.

For sandbox/demo usage, use a `biteship_test.` token and keep `BITESHIP_ALLOW_LIVE=0`. Production
requires a rotated live token and `BITESHIP_ALLOW_LIVE=1`.

## RajaOngkir checkout rates

Set `SHIPPING_RATE_PROVIDER=rajaongkir`, `RAJAONGKIR_API_KEY`, `RAJAONGKIR_ORIGIN_ID`, and
`RAJAONGKIR_COURIERS`. Checkout searches `/destination/domestic-destination`, submits the selected
location ID and total cart weight to `/calculate/domestic-cost`, and stores the returned service,
ETA, and price using the same server-bound quote protection as Biteship.

These are live RajaOngkir endpoints. When RajaOngkir is selected, provider errors are shown to the
checkout and the application does not silently substitute manually configured/demo prices.

## Production activation

1. Keep provider keys in a secret manager.
2. Verify Xendit session completion, expiry, refund, and reconciliation in test mode.
3. Verify Biteship quotes for each supported courier and destination before enabling live access.
4. Connect outbox processing to WhatsApp Business and email with retries and delivery logs.
5. Certify sandbox payments, refunds, expiry, reconciliation, and restores before launch.
