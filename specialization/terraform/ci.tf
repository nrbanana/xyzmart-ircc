# Keyless login for the release pipeline (GitHub Actions + Workload Identity Federation).
# GitHub proves which repository and branch a job runs for; Google trusts only this repository.
# No key file exists in GitHub. Created only when github_repo is set.
#
#   ircc-ci-evals   any branch of the repo   may only call Gemini (quality checks on pull requests)
#   ircc-ci-deploy  main branch only         may deploy code, shift traffic and read logs

variable "github_repo" {
  description = "owner/name of the GitHub repository allowed to use the pipeline identities, for example nrbanana/xyzmart-ircc"
  type        = string
  default     = ""
}

locals {
  ci = var.github_repo == "" ? {} : { on = true }
}

resource "google_iam_workload_identity_pool" "github" {
  for_each                  = local.ci
  workload_identity_pool_id = "github-${random_id.suffix.hex}" # a deleted pool's name is held for 30 days
  display_name              = "GitHub Actions"
  depends_on                = [google_project_service.apis]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  for_each                           = local.ci
  workload_identity_pool_id          = google_iam_workload_identity_pool.github["on"].workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  attribute_mapping = {
    "google.subject"       = "assertion.sub" # repo:<owner/name>:ref:refs/heads/<branch>
    "attribute.repository" = "assertion.repository"
  }
  attribute_condition = "assertion.repository == '${var.github_repo}'"
  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account" "ci" {
  for_each     = var.github_repo == "" ? {} : { evals = "Pipeline: quality checks", deploy = "Pipeline: deploy from main" }
  account_id   = "ircc-ci-${each.key}"
  display_name = each.value
  depends_on   = [google_project_service.apis]
}

locals {
  pool = var.github_repo == "" ? "" : google_iam_workload_identity_pool.github["on"].name
  ci_users = var.github_repo == "" ? {} : {
    evals  = "principalSet://iam.googleapis.com/${local.pool}/attribute.repository/${var.github_repo}"
    deploy = "principal://iam.googleapis.com/${local.pool}/subject/repo:${var.github_repo}:ref:refs/heads/main"
  }
}

resource "google_service_account_iam_member" "ci_login" {
  for_each           = local.ci_users
  service_account_id = google_service_account.ci[each.key].name
  role               = "roles/iam.workloadIdentityUser"
  member             = each.value
}

resource "google_project_iam_member" "ci" {
  for_each = var.github_repo == "" ? {} : {
    "evals roles/aiplatform.user"      = ["evals", "roles/aiplatform.user"]      # call Gemini and the judge model
    "deploy roles/run.sourceDeveloper" = ["deploy", "roles/run.sourceDeveloper"] # build from source, deploy, call the candidate
    "deploy roles/logging.viewer"      = ["deploy", "roles/logging.viewer"]      # watch errors during the 10% stage
  }
  project = var.project_id
  role    = each.value[1]
  member  = google_service_account.ci[each.value[0]].member
}

# Deploying a service means running it as its service account, so the deploy identity may act
# as these four accounts only.
resource "google_service_account_iam_member" "ci_deploy_acts_as" {
  for_each           = var.github_repo == "" ? toset([]) : toset(["agent", "tool-service", "supplier-stub", "build"])
  service_account_id = google_service_account.sa[each.key].name
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.ci["deploy"].member
}

output "github_settings" {
  description = "Repository variables for .github/workflows/release.yml"
  value = var.github_repo == "" ? null : {
    GCP_PROJECT      = var.project_id
    GCP_REGION       = var.region
    GCP_WIF_PROVIDER = google_iam_workload_identity_pool_provider.github["on"].name
    GCP_EVALS_SA     = google_service_account.ci["evals"].email
    GCP_DEPLOY_SA    = google_service_account.ci["deploy"].email
    GCP_BUILD_SA     = google_service_account.sa["build"].id
  }
}
