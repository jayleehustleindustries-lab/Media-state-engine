from __future__ import annotations

import json
from hashlib import sha256
import hmac
from pathlib import Path
from uuid import uuid4

import pytest

from mcp_servers.job_command_pipeline_policy import (
    JobCommandEvent,
    canonical_event_payload,
    idempotency_key,
    verify_hmac,
)


def _event(**overrides):
    payload = {
        "event_id": str(uuid4()),
        "event_type": "landing_page.cta_clicked",
        "campaign_id": str(uuid4()),
        "occurred_at": "2026-09-28T04:00:00Z",
        "source": "vercel",
        "visitor_ref": "session:opaque-123",
        "page_url": "https://job-command.example/",
        "route": "/",
        "consent_to_capture": False,
        "metadata": {"campaign_source": "launch"},
    }
    payload.update(overrides)
    return payload


def test_job_command_event_has_stable_idempotency_key():
    event = JobCommandEvent.model_validate(_event())
    assert canonical_event_payload(event) == canonical_event_payload(event)
    assert len(idempotency_key(event)) == 64


def test_job_command_event_rejects_cross_pipeline_data():
    with pytest.raises(ValueError, match="JayLeeFit"):
        JobCommandEvent.model_validate(_event(metadata={"jayleefit_job_id": "unsafe"}))


def test_capture_event_requires_explicit_capture_consent():
    with pytest.raises(ValueError, match="capture requires"):
        JobCommandEvent.model_validate(_event(event_type="campaign.capture_requested"))


def test_hmac_verification_accepts_valid_signature_and_rejects_invalid():
    body = json.dumps(_event()).encode()
    secret = "test-secret"
    signature = "sha256=" + hmac.new(secret.encode(), body, sha256).hexdigest()
    verify_hmac(body, signature, secret)
    with pytest.raises(ValueError, match="Invalid ingress signature"):
        verify_hmac(body, "sha256=not-valid", secret)


def test_grok_code_healer_refuses_repositories_outside_allowed_root(monkeypatch, tmp_path):
    from mcp_servers.grok_code_healer import CodeHealerError, resolve_repo

    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    (outside / ".git").mkdir()
    monkeypatch.setenv("CODE_HEAL_ALLOWED_ROOT", str(allowed))
    with pytest.raises(CodeHealerError, match="outside"):
        resolve_repo(str(outside))


def test_mcp_packages_keep_pipelines_separate():
    root = Path(__file__).resolve().parents[1]
    policy = (root / "mcp_servers" / "job_command_pipeline_policy.py").read_text()
    terraform = (root / "mcp_servers" / "terraform" / "job_command_vertex_pipeline.tf").read_text()
    assert "jayleefit_job_id" in policy
    assert "job-command-campaign-events" in terraform
    assert "media_state" not in terraform.lower()


def test_vertex_mcp_imports_as_a_package():
    from mcp_servers.vertex_job_command_mcp import mcp

    assert mcp.name == "Job Command Vertex Campaign Pipeline"
