#!/usr/bin/env bash
# Removes everything `make deploy` created, then lists anything that could still cost money.
set -uo pipefail
cd "$(dirname "$0")/.."

PROJECT=${PROJECT:-$(gcloud config get-value project 2>/dev/null)}
REGION=${REGION:-australia-southeast1}
BILLING=$(gcloud billing projects describe "$PROJECT" --format='value(billingAccountName)' | sed 's#billingAccounts/##')
TF="terraform -chdir=specialization/terraform"
VARS=(-var "project_id=$PROJECT" -var "region=$REGION" -var "billing_account=$BILLING")

echo "== 1/2 Terraform destroy"
# Google can take a few minutes to release the Cloud SQL network link after the database
# is deleted, so retry a few times.
for attempt in 1 2 3; do
  $TF destroy -input=false -auto-approve "${VARS[@]}" && break
  echo "   Some resources are still being released, retrying in 90 seconds ($attempt/3)"
  sleep 90
done

echo "== 2/2 Things created outside Terraform by 'gcloud run deploy --source'"
gcloud artifacts repositories delete cloud-run-source-deploy --location "$REGION" --project "$PROJECT" --quiet 2>/dev/null \
  && echo "   deleted image repository cloud-run-source-deploy"
for bucket in $(gcloud storage buckets list --project "$PROJECT" --format='value(name)' 2>/dev/null \
                | grep -E "^run-sources-|_cloudbuild$"); do
  gcloud storage rm -r "gs://$bucket" --quiet >/dev/null 2>&1 && echo "   deleted bucket $bucket"
done

echo
scripts/cost_check.sh
