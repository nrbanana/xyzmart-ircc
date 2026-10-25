# API front door (Apigee). Paid, and slow to set up, so off by default (enable_apigee = false);
# the demo uses Cloud Run's own sign-in instead.
#
# Store and web shop systems call https://<front door>/v1/stockouts with their own API key.
# Apigee checks the key, limits each caller's rate, rejects anything that breaks the stock-out
# contract, then publishes to the stockout-events topic as its own service account. Callers never
# get a Google Cloud identity. The proxy itself is in specialization/apigee/stockout-proxy and is
# deployed with: apigeecli apis create bundle -f apiproxy -n stockout, then apis deploy with --sa
# set to the front door account below.

variable "enable_apigee" {
  type    = bool
  default = false
}

resource "google_project_service" "apigee" {
  count              = var.enable_apigee ? 1 : 0
  service            = "apigee.googleapis.com"
  disable_on_destroy = false
}

resource "google_apigee_organization" "main" {
  count              = var.enable_apigee ? 1 : 0
  project_id         = var.project_id
  analytics_region   = var.region
  runtime_type       = "CLOUD"
  billing_type       = "PAYG"
  authorized_network = google_compute_network.main.id # reached over the private services link (network.tf)
  depends_on         = [google_project_service.apigee, google_service_networking_connection.private_services]
}

resource "google_apigee_instance" "main" {
  count    = var.enable_apigee ? 1 : 0
  name     = "ircc"
  location = var.region
  org_id   = google_apigee_organization.main[0].id
}

resource "google_apigee_environment" "prod" {
  count  = var.enable_apigee ? 1 : 0
  name   = "prod"
  org_id = google_apigee_organization.main[0].id
}

resource "google_apigee_instance_attachment" "prod" {
  count       = var.enable_apigee ? 1 : 0
  instance_id = google_apigee_instance.main[0].id
  environment = google_apigee_environment.prod[0].name
}

# The identity the proxy uses to publish. It can publish stock-out events and nothing else.
resource "google_service_account" "front_door" {
  count        = var.enable_apigee ? 1 : 0
  account_id   = "ircc-front-door"
  display_name = "API front door: publishes stock-out events"
}

resource "google_pubsub_topic_iam_member" "front_door_publishes" {
  count  = var.enable_apigee ? 1 : 0
  topic  = google_pubsub_topic.topic["stockout-events"].name
  role   = "roles/pubsub.publisher"
  member = google_service_account.front_door[0].member
}
