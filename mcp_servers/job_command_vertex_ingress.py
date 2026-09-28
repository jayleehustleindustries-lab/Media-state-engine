"""Cloud Run ingress for the isolated Job Command campaign event pipeline.

Deploy this separately from JayLeeFit. It verifies a Vercel/application HMAC,
validates the Job Command-only event contract, and publishes to a dedicated
Pub/Sub topic. The handler acknowledges quickly; Eventarc/Cloud Run workers do
the heavier clip-planning work asynchronously.
"""
from __future__ import annotations

import json
import os
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from google.cloud import pubsub_v1

try:  # Support both `uvicorn mcp_servers...` and direct script execution.
    from .job_command_pipeline_policy import (
        PENDING_APPROVAL, RENDER_EVENT, JobCommandEvent, idempotency_key,
        is_pending_approval, validation_error_detail, verify_hmac,
    )
    from . import job_command_campaign_store as campaign_store
except ImportError:  # pragma: no cover - direct script entry point
    from job_command_pipeline_policy import (
        PENDING_APPROVAL, RENDER_EVENT, JobCommandEvent, idempotency_key,
        is_pending_approval, validation_error_detail, verify_hmac,
    )
    import job_command_campaign_store as campaign_store


app = FastAPI(title="Job Command Vertex Ingress", version="1.0.0")


def _publisher() -> tuple[pubsub_v1.PublisherClient, str]:
    project = os.getenv("JOB_COMMAND_GCP_PROJECT", "").strip()
    topic_name = os.getenv("JOB_COMMAND_PUBSUB_TOPIC", "job-command-campaign-events").strip()
    if not project:
        raise RuntimeError("JOB_COMMAND_GCP_PROJECT is not configured.")
    if not topic_name.startswith("job-command-"):
        raise RuntimeError("JOB_COMMAND_PUBSUB_TOPIC must use the job-command- namespace.")
    client = pubsub_v1.PublisherClient()
    return client, client.topic_path(project, topic_name)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "pipeline": "job-command"}


def _pending_response(detail: str, reason: str) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={"accepted": False, "published": False, "status": PENDING_APPROVAL,
                 "reason": reason, "detail": detail},
    )


def publish_attributes(event: JobCommandEvent) -> dict[str, str]:
    attributes = {
        "event_type": event.event_type,
        "campaign_id": str(event.campaign_id),
        "event_id": str(event.event_id),
        "idempotency_key": idempotency_key(event),
        "source": event.source,
        "pipeline": "job-command",
    }
    if event.event_type == RENDER_EVENT:
        # Only reachable after verify_render_approval passed in validation.
        attributes["approval_ref"] = event.approval_ref or ""
        attributes["approval_verified"] = "true"
    return attributes


@app.post("/events")
async def receive_event(request: Request) -> Any:
    raw = await request.body()
    # PR21-F12: 401 only for signature failures.
    try:
        verify_hmac(raw, request.headers.get("x-job-command-signature"), os.getenv("JOB_COMMAND_INGRESS_SECRET", ""))
    except ValueError as exc:
        raise HTTPException(401, str(exc)) from exc
    try:
        payload = json.loads(raw)
        event = JobCommandEvent.model_validate(payload)
    except (ValueError, json.JSONDecodeError) as exc:
        if is_pending_approval(exc):
            # PR21-F3: fail closed — nothing is published without approval.
            return _pending_response(
                "campaign.render_requested requires a verified human approval record.", "approval_required"
            )
        # Never echo input values (they may contain the approval token).
        raise HTTPException(400, f"Invalid Job Command event: {validation_error_detail(exc)}") from exc

    try:
        store = campaign_store.store_from_env()
    except campaign_store.CampaignStoreNotConfigured as exc:
        # PR22-F2/F6: without the durable store there is no single-use spend or
        # dedupe, so nothing is published (fail closed).
        raise HTTPException(503, "Campaign event store is not configured.") from exc

    def _publish(payload: bytes, attributes: dict[str, str]) -> str:
        publisher, topic = _publisher()
        return publisher.publish(topic, payload, **attributes).result(timeout=20)

    try:
        result = await campaign_store.guarded_publish(
            event, publish_fn=_publish, attributes=publish_attributes(event), store=store
        )
    except campaign_store.ApprovalReplay:
        return _pending_response("This approval was already used; a new human approval is required.", "approval_spent")
    except campaign_store.PublishInProgress:
        return JSONResponse(status_code=409, content={"accepted": False, "published": False,
                                                      "status": "in_progress",
                                                      "idempotency_key": idempotency_key(event)})
    except Exception as exc:  # publishing failure must trigger Vercel retry logic
        raise HTTPException(503, "Campaign event enqueue failed.") from exc
    return {
        "accepted": True,
        "pipeline": "job-command",
        "event_id": str(event.event_id),
        "message_id": result["message_id"],
        "duplicate": result["duplicate"],
        "idempotency_key": result["idempotency_key"],
    }
