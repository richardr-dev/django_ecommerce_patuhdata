#!/usr/bin/env bash
# Run on the server as ubuntu, from /home/ubuntu/lakuin:   bash deploy/aws/switch-doku-live.sh
# Switches subscription payments from the DOKU sandbox to the live account. You type the live keys here; they are
# never shown on screen or in chat. The old .env.production is saved next to it first.
set -euo pipefail
cd "$(dirname "$0")/../.."
ENV=.env.production
[ -f "$ENV" ] || { echo "no $ENV here"; exit 1; }
read -rp "Live DOKU Client ID: " CID
read -rsp "Live DOKU Secret Key (hidden): " SECRET; echo
read -rsp "Live DOKU API key (hidden, Enter to keep the current one): " APIKEY; echo
[ -n "$CID" ] && [ -n "$SECRET" ] || { echo "Client ID and Secret Key are required."; exit 1; }
cp "$ENV" "$ENV.bak-$(date +%Y%m%d-%H%M%S)"
DOKU_CID="$CID" DOKU_SECRET="$SECRET" DOKU_APIKEY="$APIKEY" python3 - "$ENV" <<'PY'
import os, re, sys
path = sys.argv[1]
want = {"DOKU_CLIENT_ID": os.environ["DOKU_CID"], "DOKU_SECRET_KEY": os.environ["DOKU_SECRET"],
        "DOKU_API_URL": "https://api.doku.com", "PAYMENT_PROVIDER": "doku", "ALLOW_DEMO_PAYMENTS": "0"}
if os.environ.get("DOKU_APIKEY"):
    want["DOKU_API_KEY"] = os.environ["DOKU_APIKEY"]
lines, seen = open(path).read().splitlines(), set()
out = []
for line in lines:
    m = re.match(r"^([A-Z0-9_]+)=", line)
    if m and m.group(1) in want:
        if m.group(1) in seen:
            continue            # drop duplicates, the first one is replaced below
        seen.add(m.group(1)); out.append(f"{m.group(1)}={want[m.group(1)]}")
    else:
        out.append(line)
for k, v in want.items():
    if k not in seen:
        out.append(f"{k}={v}")
open(path, "w").write("\n".join(out) + "\n")
PY
docker compose -f compose.prod.yaml --env-file "$ENV" up -d web worker books </dev/null
for i in $(seq 1 40); do curl -sf https://commerce.patuhdata.id/healthz/ >/dev/null && break; sleep 3; done
echo "--- live check (opens a Rp10.000 test page, nothing is charged unless somebody pays it)"
docker compose -f compose.prod.yaml --env-file "$ENV" exec -T web python manage.py shell </dev/null <<'PY' 2>&1 | grep -v "imported automatically"
from django.conf import settings
from apps.integrations.payments import DokuPaymentProvider, doku_environment
print("environment:", doku_environment(), "| provider:", settings.PAYMENT_PROVIDER)
try:
    print("DOKU accepted the keys:", bool(DokuPaymentProvider().check_credentials()))
except Exception as e:
    print("DOKU refused:", type(e).__name__, str(e)[:200])
PY
echo "Done. Also register https://commerce.patuhdata.id/integrations/webhooks/doku/ in the LIVE DOKU dashboard (each payment method)."
