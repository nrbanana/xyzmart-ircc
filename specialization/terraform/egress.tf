# Controlled exit to suppliers (Secure Web Proxy). Paid per hour, so off by default
# (enable_secure_web_proxy = false); the demo uses Cloud NAT alone.
#
# How a call to an external supplier (SAP, Salesforce, a vendor API) leaves Google Cloud:
#   Cloud Run connector (all traffic into the private network)
#     -> this proxy: only the named supplier hosts are allowed, everything else is refused
#     -> Cloud NAT: one fixed set of public addresses the supplier can allow-list
# Credentials come from Secret Manager at call time. The security boundary (org_controls.tf) does not
# apply here: it protects Google APIs, not ordinary internet traffic.
#
# The proxy runs in explicit mode: the connector sets HTTPS_PROXY=http://10.10.0.250:443.

variable "enable_secure_web_proxy" {
  type    = bool
  default = false
}

variable "supplier_hosts" {
  description = "The only internet hosts the system may reach"
  type        = list(string)
  default     = ["api.supplier.example.com"]
}

resource "google_compute_subnetwork" "proxy_only" {
  count         = var.enable_secure_web_proxy ? 1 : 0
  name          = "ircc-proxy-only"
  network       = google_compute_network.main.id
  ip_cidr_range = "10.10.1.0/24"
  purpose       = "REGIONAL_MANAGED_PROXY"
  role          = "ACTIVE"
}

resource "google_network_security_gateway_security_policy" "suppliers" {
  count    = var.enable_secure_web_proxy ? 1 : 0
  name     = "ircc-suppliers"
  location = var.region
}

# One allow rule. Anything that matches no rule is refused.
resource "google_network_security_gateway_security_policy_rule" "allow_suppliers" {
  count                   = var.enable_secure_web_proxy ? 1 : 0
  name                    = "allow-named-suppliers"
  location                = var.region
  gateway_security_policy = google_network_security_gateway_security_policy.suppliers[0].name
  enabled                 = true
  priority                = 1000
  basic_profile           = "ALLOW"
  session_matcher         = join(" || ", [for h in var.supplier_hosts : "host() == '${h}'"])
}

resource "google_network_services_gateway" "egress_proxy" {
  count                                = var.enable_secure_web_proxy ? 1 : 0
  name                                 = "ircc-egress-proxy"
  location                             = var.region
  type                                 = "SECURE_WEB_GATEWAY"
  addresses                            = ["10.10.0.250"]
  ports                                = [443]
  gateway_security_policy              = google_network_security_gateway_security_policy.suppliers[0].id
  network                              = google_compute_network.main.id
  subnetwork                           = google_compute_subnetwork.run.id
  delete_swg_autogen_router_on_destroy = true
  depends_on                           = [google_compute_subnetwork.proxy_only]
}
