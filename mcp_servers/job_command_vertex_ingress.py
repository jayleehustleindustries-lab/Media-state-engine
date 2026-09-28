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
from google.cloud import pubsub_v1

try:  # Support both `uvicorn mcp_servers...` and direct script execution.
    from .job_command_pipeline_policy import JobCommandEvent, canonical_event_payload, idempotency_key, verify_hmac
except ImportError:  # pragma: no cover - direct script entry point
    from job_command_pipeline_policy import JobCommandEvent, canonical_event_payload, idempotency_key, verify_hmac


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


@app.post("/events")
async def receive_event(request: Request) -> dict[str, Any]:
    raw = await request.body()
    try:
        verify_hmac(raw, request.headers.get("x-job-command-signature"), os.getenv("JOB_COMMAND_INGRESS_SECRET", ""))
        payload = json.loads(raw)
        event = JobCommandEvent.model_validate(payload)
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(401, str(exc)) from exc

    try:
        publisher, topic = _publisher()
        future = publisher.publish(
            topic,
            canonical_event_payload(event),
            event_type=event.event_type,
            campaign_id=str(event.campaign_id),
            event_id=str(event.event_id),
            idempotency_key=idempotency_key(event),
            source=event.source,
            pipeline="job-command",
        )
        message_id = future.result(timeout=20)
    except Exception as exc:  # publishing failure must trigger Vercel retry logic
        raise HTTPException(503, "Campaign event enqueue failed.") from exc
    return {
        "accepted": True,
        "pipeline": "job-command",
        "event_id": str(event.event_id),
        "message_id": message_id,
        "idempotency_key": idempotency_key(event),
    }
