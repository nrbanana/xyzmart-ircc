# Private network. Nothing internal has a public address:
# - Cloud Run sends all its traffic into this subnet (Direct VPC egress)
# - Google APIs (Gemini, BigQuery, Pub/Sub) are reached privately (Private Google Access)
# - Cloud SQL has a private address only (private services access)
# - the only way out to the internet is Cloud NAT, with one set of addresses a supplier can allow

resource "google_compute_network" "main" {
  name                    = "ircc-net"
  auto_create_subnetworks = false
  depends_on              = [google_project_service.apis]
}

resource "google_compute_subnetwork" "run" {
  name                     = "ircc-run"
  network                  = google_compute_network.main.id
  ip_cidr_range            = "10.10.0.0/24"
  private_ip_google_access = true
}

resource "google_compute_router" "main" {
  name    = "ircc-router"
  network = google_compute_network.main.id
}

resource "google_compute_router_nat" "main" {
  name                               = "ircc-nat"
  router                             = google_compute_router.main.name
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"
}

resource "google_compute_global_address" "private_services" {
  name          = "ircc-private-services"
  network       = google_compute_network.main.id
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 20
}

resource "google_service_networking_connection" "private_services" {
  network                 = google_compute_network.main.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_services.name]
}
