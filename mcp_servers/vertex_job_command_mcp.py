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
from pydantic import ValidationError

try:  # Support both `python file.py` and `python -m mcp_servers.module`.
    from .job_command_pipeline_policy import (
        PENDING_APPROVAL, RENDER_EVENT, JobCommandEvent, idempotency_key,
        is_pending_approval, public_event_dict,
    )
    from . import job_command_campaign_store as campaign_store
except ImportError:  # pragma: no cover - direct script entry point
    from job_command_pipeline_policy import (
        PENDING_APPROVAL, RENDER_EVENT, JobCommandEvent, idempotency_key,
        is_pending_approval, public_event_dict,
    )
    import job_command_campaign_store as campaign_store


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


def _attributes(event: JobCommandEvent) -> dict[str, str]:
    attributes = {
        "event_type": event.event_type,
        "campaign_id": str(event.campaign_id),
        "event_id": str(event.event_id),
        "idempotency_key": idempotency_key(event),
        "source": event.source,
        "pipeline": "job-command",
    }
    if event.event_type == RENDER_EVENT:
        attributes["approval_ref"] = event.approval_ref or ""
        attributes["approval_verified"] = "true"
    return attributes


async def publish_event(event: JobCommandEvent, *, store: "campaign_store.CampaignStore | None" = None) -> dict[str, Any]:
    """Publish via the durable guards: dedupe + single-use approval spend.

    The published body is ``canonical_event_payload`` (approval_token stripped).
    """
    project, topic_name = _settings()
    topic_holder: dict[str, str] = {}

    def _publish(payload: bytes, attributes: dict[str, str]) -> str:
        publisher = pubsub_v1.PublisherClient()
        topic = publisher.topic_path(project, topic_name)
        topic_holder["topic"] = topic
        return publisher.publish(topic, payload, **attributes).result(timeout=20)

    result = await campaign_store.guarded_publish(
        event, publish_fn=_publish, attributes=_attributes(event), store=store
    )
    return {
        "pipeline": "job-command",
        "topic": topic_holder.get("topic"),
        "message_id": result["message_id"],
        "duplicate": result["duplicate"],
        "event_id": str(event.event_id),
        "idempotency_key": result["idempotency_key"],
        "next": "Cloud Run workers may claim this event. Render events are only published after a verified, single-use human approval record.",
    }


@mcp.tool()
def validate_campaign_event(event: dict[str, Any]) -> dict[str, Any]:
    """Validate isolation, consent, schema, and idempotency without publishing anything."""
    parsed = JobCommandEvent.model_validate(event)
    return {
        "valid": True,
        "pipeline": "job-command",
        "event": public_event_dict(parsed),  # never echo the approval token
        "idempotency_key": idempotency_key(parsed),
        "separation": "No JayLeeFit / Media State Engine identifiers are accepted.",
    }


@mcp.tool()
async def publish_campaign_event(event: dict[str, Any]) -> dict[str, Any]:
    """Publish one validated event to the dedicated Job Command Pub/Sub topic.

    ``campaign.render_requested`` without a verified approval record is never
    published; it is returned as ``pending_approval`` (PR21-F3, fail closed).
    """
    try:
        parsed = JobCommandEvent.model_validate(event)
    except ValidationError as exc:
        if is_pending_approval(exc):
            return {
                "published": False,
                "status": PENDING_APPROVAL,
                "pipeline": "job-command",
                "detail": "campaign.render_requested requires a verified human approval record.",
            }
        raise
    try:
        return await publish_event(parsed)
    except campaign_store.ApprovalReplay:
        return {
            "published": False,
            "status": PENDING_APPROVAL,
            "pipeline": "job-command",
            "detail": "This approval was already used; a new human approval is required.",
        }
    except campaign_store.PublishInProgress:
        return {"published": False, "status": "in_progress", "pipeline": "job-command"}


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
