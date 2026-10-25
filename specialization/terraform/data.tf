# The three data sources the agent's tools read.

# ---- Live stock and supplier orders: Cloud SQL (Postgres), private address only ----

resource "google_sql_database_instance" "main" {
  name                = "ircc-${random_id.suffix.hex}"
  database_version    = "POSTGRES_16"
  deletion_protection = false
  depends_on          = [google_service_networking_connection.private_services]

  settings {
    tier              = "db-f1-micro" # smallest instance, the main running cost of the demo
    edition           = "ENTERPRISE"
    availability_type = "ZONAL"
    disk_size         = 10
    ip_configuration {
      ipv4_enabled    = false
      private_network = google_compute_network.main.id
    }
    backup_configuration {
      enabled = false # demo data is re-seeded on start
    }
  }
}

resource "google_sql_database" "ircc" {
  name     = "ircc"
  instance = google_sql_database_instance.main.name
}

resource "random_password" "db" {
  length  = 24
  special = false
}

resource "google_sql_user" "app" {
  name     = "ircc_app"
  instance = google_sql_database_instance.main.name
  password = random_password.db.result
}

# ---- Customer loyalty: BigQuery, with personal data kept out of the agent's reach ----
# ircc_private holds the full customer table. ircc_shared holds a view without surname,
# email or card. The tool service can read only ircc_shared; the view is "authorized" to
# read ircc_private on its behalf, so no query the agent triggers can reach personal data.

resource "google_bigquery_dataset" "private" {
  dataset_id                 = "ircc_private"
  location                   = var.region
  delete_contents_on_destroy = true
  depends_on                 = [google_project_service.apis]
}

resource "google_bigquery_dataset" "shared" {
  dataset_id                 = "ircc_shared"
  location                   = var.region
  delete_contents_on_destroy = true
  depends_on                 = [google_project_service.apis]
}

resource "google_bigquery_table" "customers" {
  dataset_id          = google_bigquery_dataset.private.dataset_id
  table_id            = "customers"
  deletion_protection = false
  schema = jsonencode([
    { name = "customer_id", type = "STRING", mode = "REQUIRED" },
    { name = "first_name", type = "STRING" },
    { name = "last_name", type = "STRING" },
    { name = "email", type = "STRING" },
    { name = "card_last4", type = "STRING" },
    { name = "tier", type = "STRING" },
    { name = "spend_12m", type = "FLOAT" },
    { name = "order_note", type = "STRING" },
  ])
}

resource "google_bigquery_table" "loyalty_view" {
  dataset_id          = google_bigquery_dataset.shared.dataset_id
  table_id            = "loyalty_view"
  deletion_protection = false
  view {
    query          = "SELECT customer_id, first_name, tier, spend_12m, order_note FROM `${var.project_id}.ircc_private.customers`"
    use_legacy_sql = false
  }
  depends_on = [google_bigquery_table.customers]
}

resource "google_bigquery_dataset_access" "authorize_view" {
  dataset_id = google_bigquery_dataset.private.dataset_id
  view {
    project_id = var.project_id
    dataset_id = google_bigquery_dataset.shared.dataset_id
    table_id   = google_bigquery_table.loyalty_view.table_id
  }
}

# ---- Similar products: Vertex AI Search over the catalogue ----
# Only offered in global, us or eu locations, not Sydney. Documents are loaded by
# proto/data/seed_cloud.py. Names carry the random suffix: a deleted data store keeps its
# name for a few hours, which would block an immediate redeploy.

resource "google_discovery_engine_data_store" "catalog" {
  location          = "global"
  data_store_id     = "ircc-catalog-${random_id.suffix.hex}"
  display_name      = "xyzMart catalogue"
  industry_vertical = "GENERIC"
  content_config    = "NO_CONTENT"
  solution_types    = ["SOLUTION_TYPE_SEARCH"]
  depends_on        = [google_project_service.apis]
}

resource "google_discovery_engine_search_engine" "catalog" {
  engine_id      = "ircc-catalog-${random_id.suffix.hex}"
  collection_id  = "default_collection"
  location       = "global"
  display_name   = "xyzMart catalogue search"
  data_store_ids = [google_discovery_engine_data_store.catalog.data_store_id]
  search_engine_config {
    search_tier = "SEARCH_TIER_STANDARD"
  }
}
