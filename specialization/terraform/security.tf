# Identities, secrets and the budget.
# Each service runs as its own service account with only the access it needs.
# No service account keys exist anywhere.

locals {
  service_accounts = {
    "agent"         = "Recovery agent: calls Gemini and the tool service"
    "tool-service"  = "Tool service: the only identity that reads data or places orders"
    "supplier-stub" = "Pretend supplier"
    "workflow"      = "Supplier order workflow"
    "pubsub-push"   = "Pub/Sub push deliveries to Cloud Run"
    "build"         = "Builds the services from source"
  }
}

resource "google_service_account" "sa" {
  for_each     = local.service_accounts
  account_id   = "ircc-${each.key}"
  display_name = each.value
  depends_on   = [google_project_service.apis]
}

# ---- project-level roles (the services below have no narrower resource to grant on) ----

locals {
  project_roles = [
    ["agent", "roles/aiplatform.user"],               # call Gemini
    ["tool-service", "roles/bigquery.jobUser"],       # run queries (data access is per dataset below)
    ["tool-service", "roles/discoveryengine.viewer"], # search the catalogue
    ["tool-service", "roles/workflows.invoker"],      # start orders, send approvals
    ["build", "roles/run.builder"],                   # build images from source
  ]
}

resource "google_project_iam_member" "roles" {
  for_each = { for r in local.project_roles : "${r[0]} ${r[1]}" => r }
  project  = var.project_id
  role     = each.value[1]
  member   = google_service_account.sa[each.value[0]].member
}

# ---- data access ----

resource "google_bigquery_dataset_iam_member" "tool_reads_shared" {
  dataset_id = google_bigquery_dataset.shared.dataset_id
  role       = "roles/bigquery.dataViewer"
  member     = google_service_account.sa["tool-service"].member
}

resource "google_pubsub_topic_iam_member" "publishers" {
  for_each = {
    # In production the web shop's own service account publishes stock-out events. In the demo,
    # the agent service plays the web shop ("Customer buys", "Send the same event again").
    "agent stockout-events" = ["agent", "stockout-events"]
    "agent customer-offers" = ["agent", "customer-offers"]
  }
  topic  = google_pubsub_topic.topic[each.value[1]].name
  role   = "roles/pubsub.publisher"
  member = google_service_account.sa[each.value[0]].member
}

# ---- who may call which Cloud Run service ----

resource "google_cloud_run_v2_service_iam_member" "invokers" {
  for_each = {
    "pubsub-push agent"        = ["pubsub-push", "agent"]
    "pubsub-push tool-service" = ["pubsub-push", "tool-service"] # budget kill switch
    "agent tool-service"       = ["agent", "tool-service"]
    "agent supplier-stub"      = ["agent", "supplier-stub"] # demo "Supplier down" switch
    "workflow tool-service"    = ["workflow", "tool-service"]
    "workflow supplier-stub"   = ["workflow", "supplier-stub"]
  }
  name   = google_cloud_run_v2_service.svc[each.value[1]].name
  role   = "roles/run.invoker"
  member = google_service_account.sa[each.value[0]].member
}

# ---- secrets, encrypted with xyzMart's own key (CMEK) ----

resource "google_kms_key_ring" "main" {
  name       = "ircc-${random_id.suffix.hex}" # key rings can never be deleted, so not reused
  location   = var.region
  depends_on = [google_project_service.apis]
}

resource "google_kms_crypto_key" "secrets" {
  name            = "secrets"
  key_ring        = google_kms_key_ring.main.id
  rotation_period = "7776000s" # 90 days
}

resource "google_project_service_identity" "secretmanager" {
  provider = google-beta
  service  = "secretmanager.googleapis.com"
}

resource "google_kms_crypto_key_iam_member" "secretmanager_uses_key" {
  crypto_key_id = google_kms_crypto_key.secrets.id
  role          = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member        = google_project_service_identity.secretmanager.member
}

resource "random_password" "supplier_key" {
  length  = 32
  special = false
}

locals {
  secrets = {
    "db-password"      = { value = random_password.db.result, readers = ["tool-service"] }
    "supplier-api-key" = { value = random_password.supplier_key.result, readers = ["supplier-stub", "workflow"] }
  }
  secret_readers = merge([for name, s in local.secrets : { for r in s.readers : "${name} ${r}" => [name, r] }]...)
}

resource "google_secret_manager_secret" "secret" {
  for_each  = local.secrets
  secret_id = each.key
  replication {
    user_managed {
      replicas {
        location = var.region
        customer_managed_encryption {
          kms_key_name = google_kms_crypto_key.secrets.id
        }
      }
    }
  }
  depends_on = [google_kms_crypto_key_iam_member.secretmanager_uses_key]
}

resource "google_secret_manager_secret_version" "secret" {
  for_each    = local.secrets
  secret      = google_secret_manager_secret.secret[each.key].id
  secret_data = each.value.value
}

resource "google_secret_manager_secret_iam_member" "readers" {
  for_each  = local.secret_readers
  secret_id = google_secret_manager_secret.secret[each.value[0]].id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.sa[each.value[1]].member
}

# ---- budget: emails billing admins at 50%, 90% and 100%, and sends every spend report to the
# budget-alerts topic. At 100% the tool service switches the agent to fixed rules (kill switch). ----

resource "google_pubsub_topic_iam_member" "billing_publishes_budget" {
  topic  = google_pubsub_topic.topic["budget-alerts"].name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:billing-budget-alert@system.gserviceaccount.com"
}

resource "google_billing_budget" "main" {
  billing_account = var.billing_account
  display_name    = "xyzMart IRCC demo"
  budget_filter {
    projects = ["projects/${data.google_project.this.number}"]
  }
  amount {
    specified_amount {
      currency_code = "AUD"
      units         = tostring(var.budget_aud)
    }
  }
  dynamic "threshold_rules" {
    for_each = [0.5, 0.9, 1.0]
    content {
      threshold_percent = threshold_rules.value
    }
  }
  all_updates_rule {
    pubsub_topic   = google_pubsub_topic.topic["budget-alerts"].id
    schema_version = "1.0"
  }
  depends_on = [google_project_service.apis, google_pubsub_topic_iam_member.billing_publishes_budget]
}
