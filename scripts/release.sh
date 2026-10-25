#!/usr/bin/env bash
# Ships new code to the running services. The pipeline runs this on every push to main, after the
# unit tests and quality checks pass. It can also be run from a laptop with your gcloud login.
#
#   tool-service, supplier-stub   straight swap (their rules are covered by the unit tests)
#   agent                         new revision at 0% traffic -> smoke test on its own address
#                                 -> 10% of traffic -> watch for errors -> 100%
#                                 Any failure puts all traffic back on the previous revision.
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT=${PROJECT:-$(gcloud config get-value project 2>/dev/null)}
REGION=${REGION:-australia-southeast1}
BUILD_SA=${BUILD_SA:-projects/$PROJECT/serviceAccounts/ircc-build@$PROJECT.iam.gserviceaccount.com}
CANARY_MINUTES=${CANARY_MINUTES:-5}
TOKEN=${ID_TOKEN:-$(gcloud auth print-identity-token)}  # the pipeline passes a token for audience "ircc-agent"
AT=(--region "$REGION" --project "$PROJECT" --quiet)

build() {  # service, folder with its main.py, extra flags
  gcloud run deploy "$1" --source proto "${AT[@]}" --build-service-account "$BUILD_SA" \
    --set-build-env-vars "GOOGLE_ENTRYPOINT=python -m uvicorn --app-dir $2 main:app --host 0.0.0.0 --port \$PORT" "${@:3}"
}

echo "== 1/4 tool-service and supplier-stub"
build tool-service tool_service & p1=$!
build supplier-stub supplier_stub & p2=$!
wait $p1 && wait $p2

echo "== 2/4 agent: new revision with no traffic"
PREVIOUS=$(gcloud run services describe agent "${AT[@]}" --format='value(status.latestReadyRevisionName)')
build agent agent --no-traffic --tag candidate
NEW=$(gcloud run services describe agent "${AT[@]}" --format='value(status.latestReadyRevisionName)')
URL=$(gcloud run services describe agent "${AT[@]}" --format=json |
  python3 -c 'import json,sys; print(next(t["url"] for t in json.load(sys.stdin)["status"]["traffic"] if t.get("tag") == "candidate"))')
echo "   previous: $PREVIOUS   new: $NEW   address: $URL"

rollback() {
  echo "!! $1. All traffic back to $PREVIOUS."
  gcloud run services update-traffic agent "${AT[@]}" --to-revisions "$PREVIOUS=100" --remove-tags candidate
  exit 1
}

echo "== 3/4 smoke test on the new revision: a false alarm (HP-300 is in stock in MEL), no order, no stock change"
EVENT="{\"event_id\": \"SMOKE-$NEW\", \"sku\": \"HP-300\", \"warehouse\": \"SYD\", \"customer_id\": \"C-1001\", \"occurred_at\": \"$(date -u +%FT%TZ)\"}"
curl -sf -m 120 -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" "$URL/pubsub/stockouts" \
  -d "{\"message\": {\"data\": \"$(printf %s "$EVENT" | base64 | tr -d '\n')\", \"messageId\": \"smoke\"}}" >/dev/null \
  || rollback "Smoke test request failed"
RESULT=$(curl -sf -m 30 -H "Authorization: Bearer $TOKEN" "$URL/api/runs/SMOKE-$NEW" |
  python3 -c 'import json,sys; r=json.load(sys.stdin); print(r["status"], r["source"], r.get("check", {}).get("offer", {}).get("offer_type"))') \
  || rollback "Could not read the smoke test result"
echo "   result: $RESULT"
[ "$RESULT" = "done ai ship_from_other_warehouse" ] || rollback "Smoke test expected 'done ai ship_from_other_warehouse'"

echo "== 4/4 10% of traffic for $CANARY_MINUTES minutes, then 100%"
gcloud run services update-traffic agent "${AT[@]}" --to-tags candidate=10
sleep $((CANARY_MINUTES * 60))
ERRORS=$(gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=agent
  AND resource.labels.revision_name=$NEW AND httpRequest.status>=500" \
  --project "$PROJECT" --freshness "$((CANARY_MINUTES + 10))m" --format='value(httpRequest.status)' | wc -l | tr -d ' ')
[ "$ERRORS" = 0 ] || rollback "$ERRORS server errors on the new revision"
gcloud run services update-traffic agent "${AT[@]}" --to-latest --remove-tags candidate
echo "   $NEW now has 100% of traffic"
