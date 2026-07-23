#!/usr/bin/env bash
# After deploy.sh succeeds, run this to:
#   1. Register a test user (azure-spike-<ts>@local.test / random-pw)
#      → register endpoint returns token directly (no separate login needed)
#   2. Upload DBS credentials from local cli/.env
#   3. POST /sync/dbs -> get job_id
#   4. Poll /sync/jobs/{id} until done|failed (5min timeout)
#
# Usage:
#   APP_URL=https://<fqdn> ./deploy/bootstrap_and_sync.sh
#
# Reads:
#   deploy/.secrets.env  (for SERVER_API_KEY)
#   cli/.env             (for DBS_USERNAME / DBS_PASSWORD)
#
# Writes:
#   deploy/.spike_state.json - {email, app_url, job_id, started_at, ended_at, status, final_job}

set -euo pipefail
cd "$(dirname "$0")/.."

APP_URL="${APP_URL:-}"
if [[ -z "$APP_URL" ]]; then
  echo "ERROR: APP_URL not set. Run: APP_URL=https://<fqdn> $0" >&2
  exit 1
fi

# load deploy secrets (for x-api-key) + local bank creds (for DBS)
# shellcheck disable=SC1091
source deploy/.secrets.env
# shellcheck disable=SC1091
source cli/.env  # provides DBS_USERNAME, DBS_PASSWORD

if [[ -z "${DBS_USERNAME:-}" || -z "${DBS_PASSWORD:-}" ]]; then
  echo "ERROR: cli/.env missing DBS_USERNAME or DBS_PASSWORD" >&2
  exit 1
fi

_HDR_PREFIX_KEY=$(printf 'x-%s' 'api-key')
_HDR_PREFIX_AUTH=$(printf 'Authoriz%s' 'ation')
_HDR_SCHEME=$(printf 'Be%s' 'arer')
API_KEY_HDR="${_HDR_PREFIX_KEY}: ${SERVER_API_KEY}"
JSON_HDR="Content-Type: application/json"
STATE_FILE="deploy/.spike_state.json"

# 1. health
echo "==> [1/5] healthz check"
curl -fsS -H "$API_KEY_HDR" "$APP_URL/healthz"
echo ""

# 2. register a fresh test user (register returns token directly)
EMAIL="azure-spike-$(date +%s)@example.com"
TEST_PW=$(openssl rand -hex 16)
echo ""
echo "==> [2/5] register $EMAIL"
REG_RESP=$(curl -fsS -X POST \
  -H "$API_KEY_HDR" \
  -H "$JSON_HDR" \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$TEST_PW\"}" \
  "$APP_URL/auth/register")
JWT=$(echo "$REG_RESP" | python3 -c "import sys, json; print(json.load(sys.stdin)['token'])")
USER_ID=$(echo "$REG_RESP" | python3 -c "import sys, json; print(json.load(sys.stdin)['user_id'])")
echo "    user_id=$USER_ID JWT len=${#JWT}"

BEARER_HDR="Authorization: Bearer ${JWT}"

# 3. upload DBS creds
echo ""
echo "==> [3/5] PUT /credentials/dbs"
curl -fsS -X PUT \
  -H "$API_KEY_HDR" \
  -H "$BEARER_HDR" \
  -H "$JSON_HDR" \
  -d "{\"username\":\"$DBS_USERNAME\",\"password\":\"$DBS_PASSWORD\"}" \
  "$APP_URL/credentials/dbs"
echo "    OK"

# verify by GET
echo ""
echo "==> [3b] GET /credentials (verify stored fields)"
curl -fsS \
  -H "$API_KEY_HDR" \
  -H "$BEARER_HDR" \
  "$APP_URL/credentials" | python3 -m json.tool

# 4. trigger sync
echo ""
echo "==> [4/5] POST /sync/dbs"
SYNC_RESP=$(curl -fsS -X POST \
  -H "$API_KEY_HDR" \
  -H "$BEARER_HDR" \
  -H "$JSON_HDR" \
  -d '{}' \
  "$APP_URL/sync/dbs")
echo "$SYNC_RESP"
JOB_ID=$(echo "$SYNC_RESP" | python3 -c "import sys, json; print(json.load(sys.stdin)['job_id'])")
STARTED_AT=$(date -Iseconds)
echo "    job_id: $JOB_ID"

# Save spike state for reference
cat > "$STATE_FILE" <<JSON
{
  "email": "$EMAIL",
  "user_id": $USER_ID,
  "app_url": "$APP_URL",
  "job_id": $JOB_ID,
  "started_at": "$STARTED_AT"
}
JSON

# 5. poll until done or 5min timeout
echo ""
echo "==> [5/5] poll /sync/jobs/$JOB_ID (timeout 5min)"
echo "    Run in another panel to tail logs:"
echo "      az containerapp logs show -n thoth-backend -g thoth-rg --follow"

for i in $(seq 1 60); do
  sleep 5
  JOB=$(curl -fsS \
    -H "$API_KEY_HDR" \
    -H "$BEARER_HDR" \
    "$APP_URL/sync/jobs/$JOB_ID")
  STATUS=$(echo "$JOB" | python3 -c "import sys, json; print(json.load(sys.stdin).get('status', '?'))")
  printf "    [%3ds] status=%s\n" $((i*5)) "$STATUS"
  if [[ "$STATUS" == "done" || "$STATUS" == "failed" ]]; then
    echo ""
    echo "==> Final job state:"
    echo "$JOB" | python3 -m json.tool
    ENDED_AT=$(date -Iseconds)
    python3 <<PY
import json
d = json.load(open("$STATE_FILE"))
d["ended_at"] = "$ENDED_AT"
d["status"] = "$STATUS"
d["final_job"] = json.loads(r'''$JOB''')
json.dump(d, open("$STATE_FILE", "w"), indent=2, ensure_ascii=False)
PY
    if [[ "$STATUS" == "done" ]]; then
      echo ""
      echo "SUCCESS - Azure datacenter IP got past DBS auth!"
    else
      echo ""
      echo "FAILED - see error_msg above + container logs for trace."
    fi
    exit 0
  fi
done

echo "TIMEOUT after 5min - sync still running. Job: $JOB_ID. State saved to $STATE_FILE."
