"""PR #21 audit blockers — Job Command campaign pipeline.

F3: campaign.render_requested requires a verified human approval record (fail closed).
F5: isolation scan covers page_url/route/visitor_ref; capture allowlist is default-deny.
F12 (low, trivial): ingress returns 401 only for HMAC failures, 400 for bad payloads.
"""
from __future__ import annotations

from hashlib import sha256
import hmac
import json
import time
from uuid import uuid4

import pytest
from pydantic import ValidationError

from mcp_servers import job_command_pipeline_policy as policy
from mcp_servers.job_command_pipeline_policy import JobCommandEvent, is_pending_approval, mint_approval_token

HOST = "job-command.example"
APPROVAL_SECRET = "approval-secret-held-by-operator-only"
INGRESS_SECRET = "ingress-secret-held-by-vercel"


@pytest.fixture
def allowlist(monkeypatch):
    monkeypatch.setenv("JOB_COMMAND_CAPTURE_ALLOWED_HOSTS", HOST)
    monkeypatch.setenv("JOB_COMMAND_CAPTURE_ALLOWED_ROUTES", "/,/how-it-works")


@pytest.fixture
def secrets(monkeypatch):
    monkeypatch.setenv("JOB_COMMAND_APPROVAL_SECRET", APPROVAL_SECRET)
    monkeypatch.setenv("JOB_COMMAND_INGRESS_SECRET", INGRESS_SECRET)


def _event(**overrides):
    payload = {
        "event_id": str(uuid4()),
        "event_type": "landing_page.cta_clicked",
        "campaign_id": str(uuid4()),
        "occurred_at": "2026-09-28T04:00:00Z",
        "source": "vercel",
        "visitor_ref": "session:opaque-123",
        "page_url": f"https://{HOST}/how-it-works",
        "route": "/how-it-works",
        "consent_to_capture": True,
        "metadata": {"campaign_source": "launch"},
    }
    payload.update(overrides)
    return payload


def _render(campaign_id=None, **token_kw):
    campaign_id = campaign_id or str(uuid4())
    approval_id = token_kw.pop("approval_id", "appr-001")
    token = mint_approval_token(
        approval_id=approval_id,
        campaign_id=token_kw.pop("token_campaign_id", campaign_id),
        approved_by=token_kw.pop("approved_by", "operator@jobcommand"),
        secret=token_kw.pop("secret", APPROVAL_SECRET),
        **token_kw,
    )
    return _event(event_type="campaign.render_requested", campaign_id=campaign_id,
                  approval_ref=approval_id, approval_token=token)


def _assert_pending(payload):
    with pytest.raises(ValidationError) as exc:
        JobCommandEvent.model_validate(payload)
    assert is_pending_approval(exc.value), exc.value


# -------------------------------------------------------------- F3 -------

def test_f3_render_without_approval_is_pending(allowlist, secrets):
    _assert_pending(_event(event_type="campaign.render_requested"))
    _assert_pending(_event(event_type="campaign.render_requested", approval_ref="appr-001"))


def test_f3_render_with_verified_approval_validates(allowlist, secrets):
    event = JobCommandEvent.model_validate(_render())
    claims = policy.verify_render_approval(event)
    assert claims["status"] == "approved" and claims["scope"] == "campaign.render"


def test_f3_render_fails_closed_without_approval_secret(allowlist, monkeypatch):
    payload = _render()
    monkeypatch.delenv("JOB_COMMAND_APPROVAL_SECRET", raising=False)
    _assert_pending(payload)


def test_f3_approval_secret_must_differ_from_ingress_secret(allowlist, monkeypatch):
    monkeypatch.setenv("JOB_COMMAND_APPROVAL_SECRET", INGRESS_SECRET)
    monkeypatch.setenv("JOB_COMMAND_INGRESS_SECRET", INGRESS_SECRET)
    # Someone holding only the ingress secret cannot mint a usable approval.
    _assert_pending(_render(secret=INGRESS_SECRET))


@pytest.mark.parametrize("mutate", [
    "forged_with_ingress_secret", "wrong_campaign", "wrong_approval_ref", "expired",
    "ttl_too_long", "tampered_claims", "garbage",
])
def test_f3_invalid_approvals_stay_pending(allowlist, secrets, mutate):
    if mutate == "forged_with_ingress_secret":
        payload = _render(secret=INGRESS_SECRET)
    elif mutate == "wrong_campaign":
        payload = _render(token_campaign_id=str(uuid4()))
    elif mutate == "wrong_approval_ref":
        payload = _render()
        payload["approval_ref"] = "appr-other"
    elif mutate == "expired":
        payload = _render(ttl_seconds=60, now=int(time.time()) - 3600)
    elif mutate == "ttl_too_long":
        payload = _render(ttl_seconds=policy.MAX_APPROVAL_TTL_SECONDS + 60)
    elif mutate == "tampered_claims":
        payload = _render()
        version, body, sig = payload["approval_token"].split(".")
        claims = json.loads(policy._b64url_decode(body))
        claims["campaign_id"] = str(uuid4())
        payload["approval_token"] = ".".join([version, policy._b64url(json.dumps(claims).encode()), sig])
    else:
        payload = _render()
        payload["approval_token"] = "not-a-token"
    _assert_pending(payload)


def test_f3_approval_requested_events_remain_publishable(allowlist, secrets):
    JobCommandEvent.model_validate(_event(event_type="campaign.approval_requested"))


def test_f3_mcp_publish_returns_pending_and_never_publishes(allowlist, secrets, monkeypatch):
    from mcp_servers import vertex_job_command_mcp as mcp_mod

    def _boom(*a, **k):
        raise AssertionError("PublisherClient must not be constructed for an unapproved render")

    monkeypatch.setattr(mcp_mod.pubsub_v1, "PublisherClient", _boom)
    result = mcp_mod.publish_campaign_event(_event(event_type="campaign.render_requested"))
    assert result == {
        "published": False,
        "status": "pending_approval",
        "pipeline": "job-command",
        "detail": "campaign.render_requested requires a verified human approval record.",
    }


class _FakeFuture:
    def result(self, timeout=None):
        return "msg-1"


class _FakePublisher:
    def __init__(self):
        self.calls = []

    def publish(self, topic, data, **attrs):
        self.calls.append((topic, data, attrs))
        return _FakeFuture()


@pytest.fixture
def ingress(monkeypatch, allowlist, secrets):
    from fastapi.testclient import TestClient
    from mcp_servers import job_command_vertex_ingress as ingress_mod

    publisher = _FakePublisher()
    monkeypatch.setattr(ingress_mod, "_publisher", lambda: (publisher, "projects/p/topics/job-command-campaign-events"))
    return TestClient(ingress_mod.app), publisher


def _post(client, payload, secret=INGRESS_SECRET, raw=None):
    body = raw if raw is not None else json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, sha256).hexdigest()
    return client.post("/events", content=body, headers={"x-job-command-signature": sig})


def test_f3_ingress_unapproved_render_is_409_pending_and_not_published(ingress):
    client, publisher = ingress
    response = _post(client, _event(event_type="campaign.render_requested"))
    assert response.status_code == 409
    assert response.json()["status"] == "pending_approval"
    assert response.json()["published"] is False
    assert publisher.calls == []


def test_f3_ingress_approved_render_publishes_with_approval_attribute(ingress):
    client, publisher = ingress
    response = _post(client, _render())
    assert response.status_code == 200, response.text
    assert len(publisher.calls) == 1
    attrs = publisher.calls[0][2]
    assert attrs["approval_verified"] == "true"
    assert attrs["approval_ref"] == "appr-001"


# -------------------------------------------------------------- F12 ------

def test_f12_ingress_status_codes(ingress):
    client, publisher = ingress
    assert _post(client, _event(), secret="wrong-secret").status_code == 401
    assert _post(client, None, raw=b"{not json").status_code == 400
    assert _post(client, _event(event_type="unknown.event")).status_code == 400
    assert _post(client, _event(metadata={"jayleefit_job_id": "x"})).status_code == 400
    assert _post(client, _event()).status_code == 200
    assert len(publisher.calls) == 1


# -------------------------------------------------------------- F5 -------

@pytest.mark.parametrize("overrides", [
    {"page_url": "https://app.jayleefit.com/"},
    {"page_url": "https://media-state-engine.up.railway.app/jobs"},
    {"route": "/media_state/jobs"},
    {"route": "/%6Aayleefit/profile"},
    {"visitor_ref": "JayLeeFit:user-9"},
    {"metadata": {"mediaStateEngineJobId": "123"}},
    {"metadata": {"heygen_video_id": "abc"}},
    {"metadata": {"mse_job_id": "abc"}},
    {"metadata": {"nested": {"ref": "Media_State job 42"}}},
    {"metadata": {"items": ["ok", "jay-lee-fit"]}},
])
def test_f5_isolation_scan_covers_urls_routes_visitor_and_nested_metadata(overrides):
    with pytest.raises(ValidationError, match="JayLeeFit or shared-pipeline"):
        JobCommandEvent.model_validate(_event(**overrides))


def test_f5_capture_denied_by_default_without_allowlist(monkeypatch):
    monkeypatch.delenv("JOB_COMMAND_CAPTURE_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("JOB_COMMAND_CAPTURE_ALLOWED_ROUTES", raising=False)
    for event_type in sorted(policy.CAPTURE_CLASS_EVENTS - {"campaign.render_requested"}):
        with pytest.raises(ValidationError, match="allowlist"):
            JobCommandEvent.model_validate(_event(event_type=event_type))


def test_f5_allowlisted_capture_is_accepted(allowlist):
    event = JobCommandEvent.model_validate(_event(event_type="campaign.capture_requested"))
    assert event.route == "/how-it-works"


@pytest.mark.parametrize("overrides,match", [
    ({"page_url": "https://evil.example/how-it-works"}, "host is not on"),
    ({"page_url": f"https://{HOST}/dashboard", "route": "/dashboard"}, "route is not on"),
    ({"page_url": f"https://{HOST}/dashboard", "route": "/how-it-works"}, "must exactly match"),
    ({"page_url": f"https://{HOST}/how-it-works?user=42"}, "query string"),
    ({"page_url": f"http://{HOST}/how-it-works"}, "https"),
    ({"page_url": f"https://{HOST}:8443/how-it-works"}, "default https port"),
    ({"page_url": f"https://user:pw@{HOST}/how-it-works"}, "credentials"),
    ({"page_url": None}, "require an allowlisted"),
    ({"route": None}, "require an allowlisted"),
])
def test_f5_capture_rejects_non_allowlisted_targets(allowlist, overrides, match):
    with pytest.raises(ValidationError, match=match):
        JobCommandEvent.model_validate(_event(event_type="campaign.capture_requested", **overrides))


def test_f5_non_allowlisted_render_rejected_even_with_valid_approval(allowlist, secrets):
    payload = _render()
    payload["page_url"] = "https://evil.example/how-it-works"
    with pytest.raises(ValidationError, match="host is not on"):
        JobCommandEvent.model_validate(payload)


def test_f5_clip_brief_refuses_non_allowlisted_page(allowlist):
    from mcp_servers.vertex_job_command_mcp import create_clip_brief

    with pytest.raises(ValidationError):
        create_clip_brief(_event(event_type="campaign.clip_brief_requested",
                                 page_url=f"https://{HOST}/account", route="/account"),
                          audience="job seekers", objective="awareness")
    brief = create_clip_brief(_event(event_type="campaign.clip_brief_requested"),
                              audience="job seekers", objective="awareness")
    assert brief["source_capture"]["route"] == "/how-it-works"
