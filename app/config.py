from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql://postgres:postgres@localhost:5432/media_state"

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
    # Phase 3: NEVER auto-advance to delivered / public post from outbox.
    # Kept for backward-compat reads; ignored for public publish. Staging uses
    # rendered → staged inside the render completion path instead.
    auto_deliver_on_outbox_success: bool = False

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


settings = Settings()
