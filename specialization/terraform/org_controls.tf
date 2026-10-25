# Organisation-only controls. They need a Google Cloud Organization, which the demo project does not
# have, so they are off by default (enable_org_controls = false) and never applied here.
# `terraform validate` checks them; an organisation applies them with its own ids.
#
# 1. Security boundary (VPC Service Controls) around every Google service the agent uses.
#    Data in these services cannot be read or copied from outside the boundary, even with a valid login.
#    Rolled out in two steps: dry run (logs what would be blocked, blocks nothing), then enforce.
#    It protects Google APIs only. Supplier calls are ordinary internet traffic: see egress.tf.
# 2. Shared VPC: the network team's host project owns the network, and this project attaches to it.

variable "enable_org_controls" {
  type    = bool
  default = false
}

variable "access_policy_id" {
  description = "The organisation's Access Context Manager policy number"
  type        = string
  default     = ""
}

variable "enforce_perimeter" {
  description = "false: dry run only (log what would be blocked). true: also enforce"
  type        = bool
  default     = false
}

variable "host_project_id" {
  description = "Shared VPC host project, owned by the network team"
  type        = string
  default     = ""
}

locals {
  perimeter_services = [for s in [
    "aiplatform", "artifactregistry", "bigquery", "cloudbuild", "cloudkms", "discoveryengine",
    "pubsub", "run", "secretmanager", "sqladmin", "storage", "workflows",
  ] : "${s}.googleapis.com"]

  # Who may come in from outside, and to what. Nobody else, whatever their permissions.
  perimeter_ingress = concat(
    var.github_repo == "" ? [] : [
      { who = google_service_account.ci["deploy"].member, services = ["run", "cloudbuild", "artifactregistry", "storage"] },
      { who = google_service_account.ci["evals"].member, services = ["aiplatform"] },
    ],
    var.enable_apigee ? [
      { who = google_service_account.front_door[0].member, services = ["pubsub"] },
    ] : [],
  )
}

resource "google_access_context_manager_service_perimeter" "ircc" {
  count                     = var.enable_org_controls ? 1 : 0
  parent                    = "accessPolicies/${var.access_policy_id}"
  name                      = "accessPolicies/${var.access_policy_id}/servicePerimeters/ircc"
  title                     = "ircc"
  perimeter_type            = "PERIMETER_TYPE_REGULAR"
  use_explicit_dry_run_spec = true

  # Dry run: always written. Cloud Audit Logs show every request that would have been blocked.
  spec {
    resources           = ["projects/${data.google_project.this.number}"]
    restricted_services = local.perimeter_services
    vpc_accessible_services {
      enable_restriction = true
      allowed_services   = ["RESTRICTED-SERVICES"]
    }
    dynamic "ingress_policies" {
      for_each = local.perimeter_ingress
      content {
        ingress_from {
          identities = [ingress_policies.value.who]
          sources {
            access_level = "*"
          }
        }
        ingress_to {
          resources = ["*"]
          dynamic "operations" {
            for_each = ingress_policies.value.services
            content {
              service_name = "${operations.value}.googleapis.com"
              method_selectors {
                method = "*"
              }
            }
          }
        }
      }
    }
  }

  # Enforced: the same rules, switched on once the dry-run logs are clean.
  dynamic "status" {
    for_each = var.enforce_perimeter ? [1] : []
    content {
      resources           = ["projects/${data.google_project.this.number}"]
      restricted_services = local.perimeter_services
      vpc_accessible_services {
        enable_restriction = true
        allowed_services   = ["RESTRICTED-SERVICES"]
      }
      dynamic "ingress_policies" {
        for_each = local.perimeter_ingress
        content {
          ingress_from {
            identities = [ingress_policies.value.who]
            sources {
              access_level = "*"
            }
          }
          ingress_to {
            resources = ["*"]
            dynamic "operations" {
              for_each = ingress_policies.value.services
              content {
                service_name = "${operations.value}.googleapis.com"
                method_selectors {
                  method = "*"
                }
              }
            }
          }
        }
      }
    }
  }
}

# Shared VPC. In an organisation the network (network.tf) would live in the host project instead,
# and Cloud Run in this project would send its traffic into the host project's subnet.
resource "google_compute_shared_vpc_host_project" "host" {
  count   = var.enable_org_controls && var.host_project_id != "" ? 1 : 0
  project = var.host_project_id
}

resource "google_compute_shared_vpc_service_project" "this" {
  count           = var.enable_org_controls && var.host_project_id != "" ? 1 : 0
  host_project    = google_compute_shared_vpc_host_project.host[0].project
  service_project = var.project_id
}
