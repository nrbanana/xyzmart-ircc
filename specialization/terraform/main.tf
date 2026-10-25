# Google Cloud setup for the xyzMart recovery agent.
# Files: main (this), network, data, messaging, security, run.
# Apply with `make deploy`, remove everything with `make destroy`.

terraform {
  required_version = ">= 1.6"
  required_providers {
    google      = { source = "hashicorp/google", version = "~> 7.0" }
    google-beta = { source = "hashicorp/google-beta", version = "~> 7.0" }
    random      = { source = "hashicorp/random", version = "~> 3.6" }
  }
  # State is kept on this laptop for the demo. A team would use a GCS bucket backend.
}

variable "project_id" {
  type = string
}

variable "region" {
  type    = string
  default = "australia-southeast1"
}

variable "billing_account" {
  description = "Billing account id, for the budget alert"
  type        = string
}

variable "budget_aud" {
  type    = number
  default = 50
}

variable "gemini_model" {
  type    = string
  default = "gemini-3.6-flash"
}

provider "google" {
  project               = var.project_id
  region                = var.region
  user_project_override = true # needed for the budget API when using your own login
  billing_project       = var.project_id
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}

data "google_project" "this" {}

# A short random suffix for names Google will not let us reuse straight after a destroy
# (Cloud SQL instances, KMS key rings).
resource "random_id" "suffix" {
  byte_length = 2
}

locals {
  apis = [
    "aiplatform", "artifactregistry", "bigquery", "billingbudgets", "cloudbuild", "cloudkms",
    "compute", "discoveryengine", "iam", "iamcredentials", "pubsub", "run", "secretmanager",
    "servicenetworking", "sqladmin", "sts", "workflowexecutions", "workflows",
  ]
  # Cloud Run addresses are predictable, so subscriptions and the workflow can refer to them.
  url = { for s in ["agent", "tool-service", "supplier-stub"] :
  s => "https://${s}-${data.google_project.this.number}.${var.region}.run.app" }
}

resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  service            = "${each.value}.googleapis.com"
  disable_on_destroy = false
}

output "agent_url" {
  value = local.url["agent"]
}

output "search_engine_id" {
  value = google_discovery_engine_search_engine.catalog.engine_id
}

output "search_data_store_id" {
  value = google_discovery_engine_data_store.catalog.data_store_id
}

output "build_service_account" {
  value = google_service_account.sa["build"].id
}
