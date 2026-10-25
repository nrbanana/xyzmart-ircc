# Speed, token and error targets from specialization/finops/slo.yaml, as alerts.
# The agent writes one log line per decision ({"message": "decision", "seconds", "tokens", "source"});
# two log-based metrics read it, so no extra code or agent is needed. Free at this volume.

variable "alert_email" {
  description = "Where alerts are emailed. Empty: alerts still open incidents in Cloud Monitoring"
  type        = string
  default     = ""
}

locals {
  decision_log = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"agent\" AND jsonPayload.message=\"decision\""
  decision_metrics = {
    decision_seconds = { field = "seconds", unit = "s", scale = 0.5 } # stock-out event to checked offer
    decision_tokens  = { field = "tokens", unit = "1", scale = 100 }  # Gemini tokens per decision
  }
}

resource "google_logging_metric" "decision" {
  for_each = local.decision_metrics
  name     = "ircc/${each.key}"
  filter   = local.decision_log
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
    unit        = each.value.unit
    labels {
      key         = "source" # ai or rules: how often the fallback is used
      value_type  = "STRING"
      description = "Who decided"
    }
  }
  value_extractor  = "EXTRACT(jsonPayload.${each.value.field})"
  label_extractors = { source = "EXTRACT(jsonPayload.source)" }
  bucket_options {
    exponential_buckets {
      num_finite_buckets = 20
      growth_factor      = 1.5
      scale              = each.value.scale
    }
  }
}

resource "google_monitoring_notification_channel" "email" {
  count        = var.alert_email == "" ? 0 : 1
  display_name = "IRCC alerts"
  type         = "email"
  labels       = { email_address = var.alert_email }
  depends_on   = [google_project_service.apis]
}

locals {
  alerts = {
    "Stock-out to offer slower than 10 s (95th percentile)" = {
      metric = "logging.googleapis.com/user/ircc/decision_seconds", aligner = "ALIGN_PERCENTILE_95", above = 10
    }
    "Tokens per decision above 6,000 (95th percentile; hard ceiling in code is 8,000)" = {
      metric = "logging.googleapis.com/user/ircc/decision_tokens", aligner = "ALIGN_PERCENTILE_95", above = 6000
    }
    "Agent server errors (more than 5 in 10 minutes)" = {
      metric  = "run.googleapis.com/request_count\" AND metric.labels.response_code_class=\"5xx"
      aligner = "ALIGN_SUM", above = 5
    }
  }
}

resource "google_monitoring_alert_policy" "slo" {
  for_each     = local.alerts
  display_name = each.key
  combiner     = "OR"
  conditions {
    display_name = each.key
    condition_threshold {
      filter          = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"agent\" AND metric.type=\"${each.value.metric}\""
      comparison      = "COMPARISON_GT"
      threshold_value = each.value.above
      duration        = "0s"
      aggregations {
        alignment_period     = "600s"
        per_series_aligner   = each.value.aligner
        cross_series_reducer = each.value.aligner == "ALIGN_SUM" ? "REDUCE_SUM" : "REDUCE_MAX"
      }
    }
  }
  notification_channels = google_monitoring_notification_channel.email[*].id
  depends_on            = [google_logging_metric.decision]
}
