"""Job Command campaign-pipeline MCP server.

This server publishes validated campaign events to a dedicated Google Cloud Pub/Sub
namespace. It does not read from or write to any JayLeeFit / Media State Engine
queue, bucket, database, or credential. A Cloud Run ingress service receives
Vercel/user events, verifies them, then publishes the same validated event.

Run locally with:
  JOB_COMMAND_GCP_PROJECT=... JOB_COMMAND_PUBSUB_TOPIC=job-command-campaign-events \
    python mcp_servers/vertex_job_command_mcp.py
"""
from __future__ import annotations

import json
import os
from typing import Any

from google.cloud import pubsub_v1
from mcp.server.fastmcp import FastMCP

try:  # Support both `python file.py` and `python -m mcp_servers.module`.
    from .job_command_pipeline_policy import JobCommandEvent, canonical_event_payload, idempotency_key
except ImportError:  # pragma: no cover - direct script entry point
    from job_command_pipeline_policy import JobCommandEvent, canonical_event_payload, idempotency_key


mcp = FastMCP("Job Command Vertex Campaign Pipeline")


class JobCommandPipelineError(RuntimeError):
    pass


def _settings() -> tuple[str, str]:
    project = os.getenv("JOB_COMMAND_GCP_PROJECT", "").strip()
    topic = os.getenv("JOB_COMMAND_PUBSUB_TOPIC", "job-command-campaign-events").strip()
    if not project:
        raise JobCommandPipelineError("JOB_COMMAND_GCP_PROJECT is not configured.")
    if not topic.startswith("job-command-"):
        raise JobCommandPipelineError("JOB_COMMAND_PUBSUB_TOPIC must use the job-command- namespace.")
    return project, topic


def publish_event(event: JobCommandEvent) -> dict[str, Any]:
    project, topic_name = _settings()
    publisher = pubsub_v1.PublisherClient()
    topic = publisher.topic_path(project, topic_name)
    payload = canonical_event_payload(event)
    future = publisher.publish(
        topic,
        payload,
        event_type=event.event_type,
        campaign_id=str(event.campaign_id),
        event_id=str(event.event_id),
        idempotency_key=idempotency_key(event),
        source=event.source,
        pipeline="job-command",
    )
    message_id = future.result(timeout=20)
    return {
        "pipeline": "job-command",
        "topic": topic,
        "message_id": message_id,
        "event_id": str(event.event_id),
        "idempotency_key": idempotency_key(event),
        "next": "Cloud Run workers may claim this event; rendering still requires the campaign approval gate.",
    }


@mcp.tool()
def validate_campaign_event(event: dict[str, Any]) -> dict[str, Any]:
    """Validate isolation, consent, schema, and idempotency without publishing anything."""
    parsed = JobCommandEvent.model_validate(event)
    return {
        "valid": True,
        "pipeline": "job-command",
        "event": parsed.model_dump(mode="json"),
        "idempotency_key": idempotency_key(parsed),
        "separation": "No JayLeeFit / Media State Engine identifiers are accepted.",
    }


@mcp.tool()
def publish_campaign_event(event: dict[str, Any]) -> dict[str, Any]:
    """Publish one validated event to the dedicated Job Command Pub/Sub topic."""
    parsed = JobCommandEvent.model_validate(event)
    return publish_event(parsed)


@mcp.tool()
def create_clip_brief(event: dict[str, Any], audience: str, objective: str) -> dict[str, Any]:
    """Create an original, approval-gated brief for authorized public app capture.

    This tool does not capture a site, call a video model, or publish media.
    """
    parsed = JobCommandEvent.model_validate(event)
    if parsed.event_type not in {"campaign.capture_requested", "campaign.clip_brief_requested"}:
        raise JobCommandPipelineError("A clip brief requires a capture_requested or clip_brief_requested event.")
    if not parsed.page_url or not parsed.route:
        raise JobCommandPipelineError("Clip briefs require the approved page_url and public route.")
    return {
        "campaign_id": str(parsed.campaign_id),
        "pipeline": "job-command",
        "source_capture": {
            "page_url": str(parsed.page_url),
            "route": parsed.route,
            "consent_to_capture": parsed.consent_to_capture,
            "privacy_rule": "Capture only an approved public or sanitized staging route; never a personalized session.",
        },
        "brief": {
            "audience": audience,
            "objective": objective,
            "format": "vertical 9:16",
            "scenes": [
                "Open on a concise, approved Job Command value statement.",
                "Show a clean, non-personalized product flow from the approved route.",
                "End with an original call to action and a review-required render handoff.",
            ],
            "exclude": [
                "JayLeeFit assets, data, queues, or credentials",
                "public-figure likeness, voice, scripts, or signature delivery",
                "personal data, authenticated dashboards, payment data, and user conversations",
                "automatic public posting",
            ],
        },
        "requires_human_approval": True,
    }


if __name__ == "__main__":
    mcp.run()
