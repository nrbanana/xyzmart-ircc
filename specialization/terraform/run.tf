# The three Cloud Run services. Terraform owns their settings (identity, network, secrets,
# who may reach them); `gcloud run deploy --source` only swaps in the code. Terraform starts
# each one with Google's hello image so the settings exist before the first build.

locals {
  common_env = {
    RUN_MODE             = "gcp"
    GOOGLE_CLOUD_PROJECT = var.project_id
  }
  services = {
    agent = {
      ingress = "INGRESS_TRAFFIC_ALL" # reached through `make open` with your login; no public access
      memory  = "1Gi"
      env = {
        GOOGLE_GENAI_USE_ENTERPRISE = "TRUE"
        GOOGLE_CLOUD_LOCATION       = "global" # Gemini 3.x is not served from Sydney
        GEMINI_MODEL                = var.gemini_model
        LLM_MODE                    = "gemini"
        TOOL_SERVICE_URL            = local.url["tool-service"]
        SUPPLIER_URL                = local.url["supplier-stub"]
      }
      secrets = {}
    }
    tool-service = {
      ingress = "INGRESS_TRAFFIC_INTERNAL_ONLY"
      memory  = "512Mi"
      env = {
        DB_HOST           = google_sql_database_instance.main.private_ip_address
        DB_NAME           = google_sql_database.ircc.name
        DB_USER           = google_sql_user.app.name
        BQ_DATASET        = google_bigquery_dataset.shared.dataset_id
        BQ_LOCATION       = var.region
        SEARCH_ENGINE_ID  = google_discovery_engine_search_engine.catalog.engine_id
        SUPPLIER_URL      = local.url["supplier-stub"]
        SUPPLIER_WORKFLOW = google_workflows_workflow.supplier_order.id
      }
      secrets = { DB_PASSWORD = "db-password" }
    }
    supplier-stub = {
      ingress = "INGRESS_TRAFFIC_INTERNAL_ONLY"
      memory  = "512Mi"
      env     = {}
      secrets = { SUPPLIER_API_KEY = "supplier-api-key" }
    }
  }
}

resource "google_cloud_run_v2_service" "svc" {
  for_each            = local.services
  name                = each.key
  location            = var.region
  ingress             = each.value.ingress
  deletion_protection = false
  # A fixed token audience, so the pipeline can call a new revision on its own tagged address.
  custom_audiences = each.key == "agent" ? ["ircc-agent"] : null

  template {
    service_account = google_service_account.sa[each.key].email
    timeout         = "300s"
    scaling {
      min_instance_count = 0
      max_instance_count = 1 # demo state (switches, run history) lives in memory
    }
    vpc_access {
      egress = "ALL_TRAFFIC"
      network_interfaces {
        network    = google_compute_network.main.id
        subnetwork = google_compute_subnetwork.run.id
      }
    }
    containers {
      image = "us-docker.pkg.dev/cloudrun/container/hello"
      resources {
        limits            = { cpu = "1", memory = each.value.memory }
        startup_cpu_boost = true
      }
      dynamic "env" {
        for_each = merge(local.common_env, each.value.env)
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = each.value.secrets
        content {
          name = env.key
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.secret[env.value].secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  lifecycle {
    # `gcloud run deploy --source` owns the image, its labels and the revision name (the release
    # pipeline names revisions when it tags a candidate).
    ignore_changes = [template[0].containers[0].image, template[0].labels, template[0].annotations,
    template[0].revision, client, client_version, build_config]
  }

  depends_on = [google_secret_manager_secret_iam_member.readers, google_secret_manager_secret_version.secret]
}
