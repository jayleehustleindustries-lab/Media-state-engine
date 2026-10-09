# Job Command-only Google Cloud event pipeline.
# Apply from a distinct GCP project and service accounts from JayLeeFit / Media
# State Engine. This code intentionally creates no cross-project IAM grants.

terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.0"
    }
  }
}

variable "project_id" { type = string }
variable "region" { type = string  default = "us-central1" }
variable "ingress_image" { type = string }
variable "orchestrator_image" { type = string }
variable "ingress_shared_secret" {
  type      = string
  sensitive = true
}

provider "google" { project = var.project_id region = var.region }

resource "google_project_service" "required" {
  for_each = toset([
    "run.googleapis.com",
    "pubsub.googleapis.com",
    "eventarc.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "aiplatform.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

resource "google_service_account" "ingress" {
  account_id   = "job-command-ingress"
  display_name = "Job Command ingress publisher"
}

resource "google_service_account" "orchestrator" {
  account_id   = "job-command-orchestrator"
  display_name = "Job Command campaign orchestrator"
}

resource "google_pubsub_topic" "campaign_events" {
  name       = "job-command-campaign-events"
  depends_on = [google_project_service.required]
  labels     = { pipeline = "job-command", isolation = "required" }
}

resource "google_pubsub_topic" "clip_briefs" {
  name       = "job-command-clip-briefs"
  depends_on = [google_project_service.required]
  labels     = { pipeline = "job-command", isolation = "required" }
}

resource "google_pubsub_topic" "render_requests" {
  name       = "job-command-render-requests"
  depends_on = [google_project_service.required]
  labels     = { pipeline = "job-command", isolation = "required", approval = "required" }
}

resource "google_secret_manager_secret" "ingress_shared_secret" {
  secret_id = "job-command-ingress-hmac"
  replication { auto {} }
}

resource "google_secret_manager_secret_version" "ingress_shared_secret" {
  secret      = google_secret_manager_secret.ingress_shared_secret.id
  secret_data = var.ingress_shared_secret
}

resource "google_secret_manager_secret_iam_member" "ingress_access" {
  secret_id = google_secret_manager_secret.ingress_shared_secret.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.ingress.email}"
}

resource "google_pubsub_topic_iam_member" "ingress_publish" {
  topic  = google_pubsub_topic.campaign_events.name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${google_service_account.ingress.email}"
}

resource "google_cloud_run_v2_service" "ingress" {
  name     = "job-command-ingress"
  location = var.region
  template {
    service_account = google_service_account.ingress.email
    containers {
      image = var.ingress_image
      env { name = "JOB_COMMAND_GCP_PROJECT" value = var.project_id }
      env { name = "JOB_COMMAND_PUBSUB_TOPIC" value = google_pubsub_topic.campaign_events.name }
      env {
        name = "JOB_COMMAND_INGRESS_SECRET"
        value_source { secret_key_ref { secret = google_secret_manager_secret.ingress_shared_secret.secret_id version = "latest" } }
      }
    }
  }
  depends_on = [google_project_service.required]
}

# Vercel must call this public ingress endpoint with a valid HMAC. The service
# validates every event before publishing; unauthenticated HTTP here does not
# imply unauthenticated processing.
resource "google_cloud_run_v2_service_iam_member" "ingress_public" {
  name     = google_cloud_run_v2_service.ingress.name
  location = google_cloud_run_v2_service.ingress.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_v2_service" "orchestrator" {
  name     = "job-command-campaign-orchestrator"
  location = var.region
  template {
    service_account = google_service_account.orchestrator.email
    containers {
      image = var.orchestrator_image
      env { name = "JOB_COMMAND_CLIP_BRIEF_TOPIC" value = google_pubsub_topic.clip_briefs.name }
      env { name = "JOB_COMMAND_RENDER_REQUEST_TOPIC" value = google_pubsub_topic.render_requests.name }
    }
  }
  depends_on = [google_project_service.required]
}

resource "google_cloud_run_v2_service_iam_member" "eventarc_invoker" {
  name     = google_cloud_run_v2_service.orchestrator.name
  location = google_cloud_run_v2_service.orchestrator.location
  role     = "roles/run.invoker"
  member   = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-eventarc.iam.gserviceaccount.com"
}

data "google_project" "current" {}

resource "google_eventarc_trigger" "campaign_events" {
  name     = "job-command-campaign-events"
  location = var.region
  matching_criteria {
    attribute = "type"
    value     = "google.cloud.pubsub.topic.v1.messagePublished"
  }
  transport {
    pubsub { topic = google_pubsub_topic.campaign_events.id }
  }
  destination {
    cloud_run_service {
      service = google_cloud_run_v2_service.orchestrator.name
      region  = var.region
    }
  }
  service_account = google_service_account.orchestrator.email
  depends_on      = [google_cloud_run_v2_service_iam_member.eventarc_invoker]
}

output "job_command_ingress_url" { value = google_cloud_run_v2_service.ingress.uri }
output "campaign_event_topic" { value = google_pubsub_topic.campaign_events.id }
