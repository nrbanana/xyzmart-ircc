# Message queues (Pub/Sub) and the supplier order workflow (Workflows).
#
#   stock-events    live sales and restocks       -> tool service (detects sell-outs)
#   sold-out-alerts a purchase could not be filled -> agent
#   customer-offers the checked offer              -> customer channels (here: an audit subscription)

resource "google_pubsub_topic" "topic" {
  for_each   = toset(["stock-events", "sold-out-alerts", "customer-offers"])
  name       = each.value
  depends_on = [google_project_service.apis]
}

locals {
  push = {
    "stock-events"    = "${local.url["tool-service"]}/pubsub/stock-events"
    "sold-out-alerts" = "${local.url["agent"]}/pubsub/alerts"
  }
}

resource "google_pubsub_subscription" "push" {
  for_each             = local.push
  name                 = "${each.key}-push"
  topic                = google_pubsub_topic.topic[each.key].id
  ack_deadline_seconds = 120 # an agent run takes about 6 seconds; leave room for retries
  push_config {
    push_endpoint = each.value
    oidc_token {
      service_account_email = google_service_account.sa["pubsub-push"].email
    }
  }
  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "300s"
  }
}

resource "google_pubsub_subscription" "offers_audit" {
  name                       = "customer-offers-audit"
  topic                      = google_pubsub_topic.topic["customer-offers"].id
  message_retention_duration = "86400s"
}

# Places the supplier order. Large orders pause until a person approves them on the demo
# screen (a Workflows callback), then the supplier is called with retries. If the supplier
# stays down, the order is marked QUEUED for a later retry.
resource "google_workflows_workflow" "supplier_order" {
  name                = "supplier-order"
  region              = var.region
  service_account     = google_service_account.sa["workflow"].id
  deletion_protection = false
  source_contents = templatefile("${path.module}/supplier_order.yaml", {
    tool_url     = local.url["tool-service"]
    supplier_url = local.url["supplier-stub"]
    project_id   = var.project_id
  })
  depends_on = [google_project_service.apis]
}
