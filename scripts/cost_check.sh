#!/usr/bin/env bash
# Lists anything in the project that could still cost money. Empty lists mean nothing is billing.
set -uo pipefail
PROJECT=${PROJECT:-$(gcloud config get-value project 2>/dev/null)}
REGION=${REGION:-australia-southeast1}

check() {  # label, command...
  local label=$1; shift
  local found
  found=$("$@" 2>/dev/null)
  printf '%-28s %s\n' "$label" "${found:-none}"
}

echo "Billable resources left in $PROJECT:"
check "Cloud Run services"      gcloud run services list --project "$PROJECT" --format='value(name)'
check "Cloud SQL instances"     gcloud sql instances list --project "$PROJECT" --format='value(name)'
check "Cloud NAT routers"       gcloud compute routers list --project "$PROJECT" --format='value(name)'
check "Workflows"               gcloud workflows list --location "$REGION" --project "$PROJECT" --format='value(name.basename())'
check "Pub/Sub topics"          gcloud pubsub topics list --project "$PROJECT" --format='value(name.basename())'
check "BigQuery datasets"       bq ls --project_id "$PROJECT" --format=sparse
check "Secrets"                 gcloud secrets list --project "$PROJECT" --format='value(name.basename())'
check "Image repositories"      gcloud artifacts repositories list --project "$PROJECT" --format='value(name.basename())'
check "Storage buckets"         gcloud storage buckets list --project "$PROJECT" --format='value(name)'
check "Search data stores"      bash -c "curl -s -H 'Authorization: Bearer $(gcloud auth print-access-token)' \
  -H 'x-goog-user-project: $PROJECT' \
  'https://discoveryengine.googleapis.com/v1/projects/$PROJECT/locations/global/collections/default_collection/dataStores' \
  | grep -o '\"displayName\": \"[^\"]*\"' | cut -d'\"' -f4"
echo
echo "Free and safe to leave: the VPC network and subnet (Google can hold the subnet for up to"
echo "2 hours after Cloud Run is deleted; run make destroy again later to remove it), KMS key"
echo "rings (cannot be deleted; key versions are destroyed), and enabled APIs."
