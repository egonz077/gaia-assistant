from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    anthropic_api_key: str
    voyage_api_key: str
    wa_access_token: str
    wa_app_secret: str
    wa_verify_token: str
    wa_phone_number_id: str

    model: str = "claude-opus-5"
    debounce_seconds: float = 3.0


settings = Settings()  # type: ignore[call-arg]
