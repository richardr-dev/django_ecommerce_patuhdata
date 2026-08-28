# Mobile API v1

The native Android/iOS API is served from `/api/v1/`. Every request must select a tenant with:

```http
X-Store-Slug: your-store-slug
```

All responses use either `{"data": ...}` or a stable error shape:

```json
{"error": {"code": "invalid_request", "message": "Human-readable message"}}
```

Money values are decimal strings, not floating-point numbers. Dates are ISO 8601 strings.

## Public storefront

- `GET /api/v1/store/` — branding, announcement, carousel slides, active campaign, loyalty flag.
- `GET /api/v1/products/?q=&category=&page=1&page_size=20` — paginated catalog with current stock and flash-sale price.
- `GET /api/v1/products/{slug}/` — product, gallery, variants, stock, and active sale details.

## Authentication

Create a token with the same customer account used by the website:

```bash
curl -X POST https://shop.example/api/v1/auth/token/ \
  -H 'Content-Type: application/json' \
  -H 'X-Store-Slug: your-store-slug' \
  -d '{"email":"buyer@example.com","password":"secret"}'
```

Send the returned token only over HTTPS:

```http
Authorization: Bearer TOKEN
```

Raw tokens are returned once and stored as SHA-256 hashes. They expire after
`MOBILE_TOKEN_TTL_DAYS` (30 by default), are scoped to one store, and can be revoked with
`POST /api/v1/auth/logout/`. Login is throttled after repeated failures.

## Customer endpoints

- `GET /api/v1/me/` — authenticated customer identity.
- `GET /api/v1/orders/` — the customer's latest 100 store-scoped orders and line items.
- `GET /api/v1/loyalty/` — points balance, merchant rules, and latest 100 ledger entries.
- `POST /api/v1/auth/logout/` — revoke the current token.

## Mobile storage and security

Store the bearer token in iOS Keychain or Android Keystore-backed encrypted storage. Never log it,
place it in a URL, or bundle merchant/customer credentials in the app. Production deployments must
use HTTPS and should use Redis as the shared cache so login throttling works consistently across
multiple web processes.
