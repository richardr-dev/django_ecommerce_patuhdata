# Caddy with the Cloudflare DNS module, so it can prove control of lakuin.store through the
# Cloudflare API and hold one wildcard certificate for every store subdomain.
# v0.2.4+ accepts Cloudflare's current token formats (cfut_/cfat_, 53+ characters); v0.2.2
# rejected them at start-up and Caddy would not run.
FROM caddy:2.10.2-builder-alpine AS builder
RUN xcaddy build --with github.com/caddy-dns/cloudflare@v0.2.4

FROM caddy:2.10.2-alpine
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
