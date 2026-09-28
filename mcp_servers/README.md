# Everyday Hustle MCPs — Grok Code Healer and Job Command Vertex Pipeline

These are **two separate systems**. They share no queue, storage bucket, database, service account, API key, or deployment target with each other or with the JayLeeFit media pipeline.

> **Scope note (PR #21 audit):** the statement above covers these two MCP/campaign components only. The Job Command *live voice* backend (`/voice/*`) currently still runs inside the Media State Engine API process, Postgres database, and worker. It is feature-flagged OFF by default and uses its own API key and provider credentials, but full infrastructure separation (own DB/worker/deployment) is a tracked follow-up.

| Component | What it does | What it cannot do |
| --- | --- | --- |
| **Grok Code Healer** | Reads an allowlisted Git working tree, runs bounded checks, and asks Grok for a structured remediation proposal. | It cannot merge, push, deploy, alter a default branch, access a repository outside its allowlisted root, or apply a patch by default. |
| **Job Command Vertex Campaign Pipeline** | Validates a signed Job Command event and publishes it to a dedicated `job-command-*` Pub/Sub topic. Eventarc delivers it to a separate Cloud Run orchestrator for approved clip planning and rendering. | It rejects JayLeeFit/Media State Engine references in metadata, `page_url`, `route`, and `visitor_ref`; only accepts capture/render targets on a config-driven host + exact-route allowlist (default deny); accepts no cross-project IAM grants; and refuses to publish `campaign.render_requested` unless it carries a verified, signed human approval record (otherwise `pending_approval`, nothing published). |

## Recommended deployment choice

| Approach | Tradeoffs | Cost | Setup complexity |
| --- | --- | --- | --- |
| **Dedicated serverless services (recommended)** | Isolated Cloud Run ingress/orchestrator, Pub/Sub, Eventarc, Secret Manager, and per-service accounts. The MCP server can run locally for operator tasks or in a separate internal service. | Provider usage; evaluate current cloud pricing before deployment. | Moderate, but it gives durable webhooks and clean separation. |
| **Local operator MCP only** | Fastest way to use Grok Code Healer during development. It stops when the machine/session stops and cannot receive a Vercel event. | Local runtime plus model/API usage. | Low; use the checked-in `.grok/config.toml`. |

The event-driven campaign flow needs the first approach. Vercel webhooks deliver `POST` events, Eventarc routes validated events, and Cloud Run is the public ingress; the handler returns quickly and the heavy work occurs asynchronously. [Vercel webhooks](https://vercel.com/docs/webhooks) [Eventarc](https://docs.cloud.google.com/eventarc/docs) [Cloud Run webhook targets](https://docs.cloud.google.com/run/docs/triggering/webhooks)

## 1. Grok Code Healer

### Why this is an MCP, not unrestricted self-healing

xAI documents MCP support and tool calling, but a model tool call does not make a safe release process. This server is designed for **review first**: it packages diagnostics, asks Grok for a JSON plan, and leaves code changes, tests, PR creation, and merge approval to the operator. [xAI MCP servers](https://docs.x.ai/build/features/mcp-servers) [xAI tools](https://docs.x.ai/developers/tools/overview)

### Run it locally

```bash
cd Media-state-engine
python3 -m pip install -r mcp_servers/requirements.txt
export XAI_API_KEY=...                 # host secret, never commit or send to a browser
export CODE_HEAL_ALLOWED_ROOT="$PWD"    # only this tree and its descendants are readable
python3 mcp_servers/grok_code_healer.py
```

The included [`.grok/config.toml`](../.grok/config.toml) provides project-scoped registration for Grok CLI. Keep `GROK_CODE_HEAL_ALLOW_WRITES=false` unless a later change adds a separate, explicitly approved patch-application workflow. The current server contains these tools:

| Tool | Safe behavior |
| --- | --- |
| `inspect_failure` | Runs Git status/diff checks and the project’s bounded typecheck or test command. No model call. |
| `audit_repository` | Adds a Grok remediation analysis, but does not change files. |
| `prepare_patch_request` | Produces a review plan only. It does not create a branch, write a patch, push, merge, or deploy. |

## 2. Job Command Vertex campaign pipeline

### Required isolation

Create a **new Google Cloud project** and separate service accounts for Job Command. Do not reuse a JayLeeFit project, bucket, BigQuery dataset, Pub/Sub topic, database, or secret. The Terraform module intentionally only creates `job-command-*` resources and has no cross-project grants.

```text
Landing-page CTA (after notice and consent)
  → Vercel server route derives an opaque session reference
  → HMAC-signed POST to Job Command Cloud Run ingress
  → validate schema, consent, isolation, and idempotency
  → Pub/Sub job-command-campaign-events
  → Eventarc
  → Job Command Cloud Run orchestrator
  → public/sanitized app capture request → clip brief → render request
  → human review/approval → distribution path
```

### Event contract

The event must have its own UUID, a Job Command campaign UUID, and an allowlisted event type. The ingress rejects personal/legacy cross-pipeline fields and only permits approved public routes for capture.

Enforced in `job_command_pipeline_policy.py` (shared by ingress and MCP):

| Control | Config | Behavior |
| --- | --- | --- |
| Capture allowlist | `JOB_COMMAND_CAPTURE_ALLOWED_HOSTS`, `JOB_COMMAND_CAPTURE_ALLOWED_ROUTES` (comma lists, exact match) | `capture_requested`, `clip_brief_requested`, `render_requested` need `https`, allowlisted host, allowlisted route, `page_url` path == `route`, no query/fragment/credentials/port. Empty config denies all. |
| Isolation scan | — | Normalized (case/punctuation/percent-decoding) scan of metadata keys+values, `page_url`, `route`, `visitor_ref` for JayLeeFit / Media State / HeyGen-id aliases. |
| Render approval | `JOB_COMMAND_APPROVAL_SECRET` (must differ from `JOB_COMMAND_INGRESS_SECRET`) | `render_requested` needs `approval_ref` + `approval_token` signed by the operator approval workflow (`mint_approval_token`), bound to the campaign, `status=approved`, unexpired, TTL ≤ 24h. Otherwise ingress returns **409 `pending_approval`** and the MCP returns `{"published": false, "status": "pending_approval"}`. |
| Single-use approvals | `JOB_COMMAND_CAMPAIGN_DATABASE_URL` (dedicated DB/role; **no** fallback to MSE `DATABASE_URL`) | Each approval is spent once in `job_command_spent_approvals` (PK sha256(token), UNIQUE(campaign_id, approval_id)), in the same transaction as the publish claim. Another event reusing it → **409 `pending_approval`** (`reason: approval_spent`). A retry of the *same* event is allowed. |
| Publish idempotency | same DSN | `job_command_publish_log` (PK `idempotency_key`) is claimed before Pub/Sub publish; a retry of a published event returns the prior `message_id` (`duplicate: true`) without republishing; a concurrent in-flight duplicate → 409 `in_progress`; stale `pending` claims (>300 s) are reclaimed. Store unset → **503, nothing published**. |
| Token hygiene | — | The verified `approval_token` is stripped from the Pub/Sub body, the idempotency hash, MCP tool responses, error messages and reprs; only its sha256 is stored. |
| Ingress status codes | — | 401 = bad HMAC only; 400 = invalid payload (no input echo); 409 = pending approval / in progress; 503 = store unset or publish failure. |

```json
{
  "event_id": "e7f60cac-6ea6-4f2d-9c22-7880c57c2aac",
  "event_type": "campaign.capture_requested",
  "campaign_id": "8db51eb6-77a6-45da-bac9-b2d8c4a2e288",
  "occurred_at": "2026-09-28T04:00:00Z",
  "source": "vercel",
  "visitor_ref": "session:opaque-id",
  "page_url": "https://your-public-job-command-site.example/",
  "route": "/how-it-works",
  "consent_to_capture": true,
  "metadata": { "campaign_source": "launch" }
}
```

**Capture rule:** only capture an explicitly approved public or sanitized staging route. Never capture a signed-in dashboard, client plan, checkout, video session, voice transcript, or any page containing personalized data. The pipeline prepares vertical 9:16 clip briefs; a separate, signed, single-use approval record is required before any render request is published. Approval signing is still symmetric HMAC (verifiers could mint); asymmetric/KMS signing is a follow-up. The Terraform module does not yet provision the campaign database.

### Deploy the isolated infrastructure

1. Build separate container images for the ingress and event orchestrator, then configure a distinct Google Cloud project and a dedicated billing account.
2. From `mcp_servers/terraform`, set `project_id`, `region`, image URIs, and `ingress_shared_secret` through a secure Terraform variable mechanism.
3. Apply [the Terraform module](terraform/job_command_vertex_pipeline.tf). It creates the Pub/Sub topics, Eventarc route, Cloud Run services, service accounts, and a Secret Manager secret.
4. Configure a **Vercel server route**, not browser code, to sign outbound events with `x-job-command-signature`. Vercel’s webhook docs specify public HTTPS targets and an `x-vercel-signature` for Vercel-originated events; verify the incoming source at the Vercel adapter, then generate the separate Job Command HMAC for the Cloud Run ingress. [Vercel webhooks](https://vercel.com/docs/webhooks)
5. Add Vercel’s actual published URL only after deciding which public pages are allowed to appear in campaign clips. Then the orchestrator can be given a safe capture allowlist.

## Secrets and connectivity

No custom Manus connector has been submitted because these servers need a stable deployment URL/command and an isolated Google project before registration. The current task already has built-in **Grok**, **Google Gemini**, **Vercel**, **ElevenLabs**, and **Fitbod** connectors enabled; creating a duplicate, unhosted custom connector would add no capability.

When a stable MCP endpoint or host command is available, register it with the custom-MCP workflow. Do not put `XAI_API_KEY`, Google credentials, Vercel webhook secret, HMAC secret, ElevenLabs secret, or browser/session tokens in source control, MCP configuration text, URLs, or client bundles.

## AI receptionist correction

There is no “ElevenLabs V3 certified” receptionist designation to rely on. The concrete, supportable setup is a private ElevenLabs Agent with documented multilingual configuration, language detection, signed session URLs, and a clear pre-microphone AI/recording disclosure. Gemini can provide live captions or post-call analysis, but neither provider should be used to identify who a person is from their voice or accent. See [Job Command live voice architecture](../docs/JOB_COMMAND_LIVE_VOICE.md).
