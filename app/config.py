from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    database_url: str = "postgresql://postgres:postgres@localhost:5432/media_state"
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""
    elevenlabs_url: str = "https://api.elevenlabs.io/v1/text-to-speech"
    remotion_render_url: str = ""
    webhook_url: str = ""
    webhook_secret: str = ""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
