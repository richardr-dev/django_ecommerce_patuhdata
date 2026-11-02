# Caddy with the Cloudflare DNS module, so it can prove control of lakuin.store through the
# Cloudflare API and hold one wildcard certificate for every store subdomain.
FROM caddy:2.10.2-builder-alpine AS builder
RUN xcaddy build --with github.com/caddy-dns/cloudflare@v0.2.2

FROM caddy:2.10.2-alpine
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
