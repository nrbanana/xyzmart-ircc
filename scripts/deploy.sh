#!/usr/bin/env bash
# Creates everything in Google Cloud and deploys the three services. Safe to run again.
# Uses your gcloud login (gcloud auth login + gcloud auth application-default login).
# Set GITHUB_REPO=owner/name to also create keyless login for the release pipeline.
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT=${PROJECT:-$(gcloud config get-value project 2>/dev/null)}
REGION=${REGION:-australia-southeast1}
BILLING=$(gcloud billing projects describe "$PROJECT" --format='value(billingAccountName)' | sed 's#billingAccounts/##')
TF="terraform -chdir=specialization/terraform"
VARS=(-var "project_id=$PROJECT" -var "region=$REGION" -var "billing_account=$BILLING" -var "github_repo=${GITHUB_REPO:-}")
[ -n "$BILLING" ] || { echo "Project $PROJECT has no billing account"; exit 1; }

echo "== 1/4 Infrastructure with Terraform (first run about 15 minutes, mostly Cloud SQL)"
$TF init -input=false >/dev/null
$TF apply -input=false -auto-approve "${VARS[@]}"
BUILD_SA=$($TF output -raw build_service_account)

echo "== 2/4 Building the three services from source (about 5 minutes)"
LOGS=$(mktemp -d)
# The three builds share one image repository. Create it first so they do not race to create it.
gcloud artifacts repositories describe cloud-run-source-deploy --location "$REGION" --project "$PROJECT" >/dev/null 2>&1 \
  || gcloud artifacts repositories create cloud-run-source-deploy --repository-format docker \
       --location "$REGION" --project "$PROJECT" --quiet
deploy() {  # service, folder with its main.py
  gcloud run deploy "$1" --source proto --region "$REGION" --project "$PROJECT" --quiet \
    --build-service-account "$BUILD_SA" \
    --set-build-env-vars "GOOGLE_ENTRYPOINT=python -m uvicorn --app-dir $2 main:app --host 0.0.0.0 --port \$PORT" \
    > "$LOGS/$1.log" 2>&1 || { echo "Deploy of $1 failed:"; tail -20 "$LOGS/$1.log"; return 1; }
  echo "   $1 deployed"
}
deploy supplier-stub supplier_stub & p1=$!
deploy tool-service tool_service & p2=$!
deploy agent agent & p3=$!
failed=0
for p in $p1 $p2 $p3; do wait $p || failed=1; done
[ $failed = 0 ] || { echo "A service failed to deploy (see above). Fix it and run make deploy again."; exit 1; }

echo "== 3/4 Loading sample customers (BigQuery) and catalogue (Vertex AI Search)"
.venv/bin/python proto/data/seed_cloud.py "$PROJECT" "$REGION" "$($TF output -raw search_data_store_id)"

echo "== 4/4 Done"
if [ -n "${GITHUB_REPO:-}" ]; then
  echo "   Pipeline settings for $GITHUB_REPO (set each with: gh variable set NAME --body VALUE --repo $GITHUB_REPO):"
  $TF output -json github_settings | python3 -c 'import json,sys; [print(f"     {k}={v}") for k, v in json.load(sys.stdin).items()]'
fi
echo "   Open the demo screen:  make open   (then http://localhost:8080)"
echo "   Remove everything:     make destroy"
