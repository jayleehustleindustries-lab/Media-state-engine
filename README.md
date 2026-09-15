# Media-state-engine
Atomic PostgreSQL state engine and media orchestration store for automated vertical video pipelines.
# Media Automation State Engine

Core database backend powering FastAPI media orchestration, ElevenLabs audio synthesis jobs, and Remotion rendering state transitions.

---

### Responsibilities
- **State Transitions:** Atomic status tracking for raw scripts, TTS audio generation, video rendering, and delivery.
- **Idempotency & Deduplication:** Prevents duplicate AI generation calls, API credit burn, and race conditions across webhooks.
- **Asset Registry:** Canonical storage for generated ElevenLabs audio URLs, Remotion asset paths, and rendered MP4 artifacts.
- **Dispatcher Sync:** Emits webhooks/events to notify downstream review dashboards upon render completion.

---

### Engine vs. Dashboard Split
- **Backend (Supabase / Postgres):** Handles high-frequency atomic state changes, webhooks, and raw asset mapping.
- **Frontend / Review (ClickUp / Notion):** Human-in-the-loop review station notified only after video rendering completes successfully.

