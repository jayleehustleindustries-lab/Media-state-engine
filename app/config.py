from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql:///media_state"

    # API auth — either name works; MEDIA_ENGINE_API_KEY takes precedence if both set
    api_key: str = ""
    media_engine_api_key: str = ""

    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""
    elevenlabs_url: str = "https://api.elevenlabs.io/v1/text-to-speech"
    elevenlabs_webhook_secret: str = ""

    heygen_api_key: str = ""
    heygen_api_url: str = "https://api.heygen.com"
    heygen_avatar_id: str = ""
    heygen_voice_id: str = ""
    heygen_webhook_secret: str = ""
    # Explicit callback override; else PUBLIC_BASE_URL + /webhooks/heygen
    heygen_callback_url: str = ""
    # Cost guardrail: second HeyGen render for 16:9 ONLY when this is true AND job opts in
    heygen_allow_dual_format: bool = False

    remotion_render_url: str = ""
    remotion_webhook_secret: str = ""

    webhook_url: str = ""
    webhook_secret: str = ""

    # Public base URL of this API (builds HeyGen callback_url when unset)
    public_base_url: str = ""

    # Local asset storage (ElevenLabs audio bytes, etc.)
    asset_storage_dir: str = "data/assets"
    # Staging packages for manual / pre-live distribute
    distribute_staging_dir: str = "data/staging"

    # Outbox delivery
    webhook_outbox_max_attempts: int = 8
    webhook_outbox_base_delay_seconds: float = 2.0

    # Idempotency: unfinished keys older than this (seconds) are cleared on read
    idempotency_ttl_seconds: int = 3600

    # Stuck-job reconcile: jobs in rendering/audio_generating older than this
    stuck_job_seconds: int = 900

    # Background worker (Postgres work_queue)
    worker_poll_interval_seconds: float = 2.0
    worker_batch_size: int = 5
    worker_max_attempts: int = 5
    worker_reconcile_interval_seconds: float = 60.0
    # Reclaim crashed claims (Phase 5 / audit F4)
    work_queue_stale_seconds: int = 900
    outbox_stale_seconds: int = 900
    # Phase 3: NEVER auto-advance to delivered / public post from outbox.
    # Kept for backward-compat reads; ignored for public publish. Staging uses
    # rendered → staged inside the render completion path instead.
    auto_deliver_on_outbox_success: bool = False

    # Phase 4 — daily cadence scheduler
    schedule_posts_per_day: int = 3
    schedule_timezone: str = "America/Los_Angeles"
    schedule_topics: str = ""  # comma-separated or JSON list
    schedule_duration_seconds: int = 30
    schedule_platforms: str = "tiktok,reels,shorts"
    # When true, schedule tick also enqueues generate_avatar (burns HeyGen when worker runs)
    schedule_enqueue_avatar: bool = False

    # Phase 4 — YouTube Shorts (OAuth). Empty = safe staging-only no-op.
    youtube_client_id: str = ""
    youtube_client_secret: str = ""
    youtube_refresh_token: str = ""
    # private | unlisted | public — default private so even live uploads are not public
    youtube_privacy_status: str = "private"

    # --- Image quality gate (hard, fail-closed) ---
    image_gate_max_attempts: int = 3
    image_gate_fail_closed: bool = True
    image_gate_break_glass: bool = False
    image_scorer_provider: str = "gemini"  # gemini | grok | mock
    image_scorer_gemini_api_key: str = ""
    gemini_api_key: str = ""
    image_scorer_gemini_model: str = "gemini-2.0-flash"
    image_scorer_grok_api_key: str = ""
    xai_api_key: str = ""
    image_scorer_grok_model: str = "grok-2-vision-1212"
    image_scorer_timeout_seconds: float = 60.0
    image_scorer_daily_budget: int = 200
    active_reference_set_path: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def effective_api_key(self) -> str:
        return (self.media_engine_api_key or self.api_key or "").strip()

    @property
    def effective_heygen_callback_url(self) -> str:
        explicit = (self.heygen_callback_url or "").strip()
        if explicit:
            return explicit
        base = (self.public_base_url or "").rstrip("/")
        if base:
            return f"{base}/webhooks/heygen"
        return ""

    @property
    def schedule_platforms_list(self) -> list[str]:
        raw = (self.schedule_platforms or "tiktok,reels,shorts").strip()
        return [p.strip() for p in raw.split(",") if p.strip()] or ["tiktok", "reels", "shorts"]

    @property
    def youtube_configured(self) -> bool:
        return bool(
            self.youtube_client_id.strip()
            and self.youtube_client_secret.strip()
            and self.youtube_refresh_token.strip()
        )


settings = Settings()
