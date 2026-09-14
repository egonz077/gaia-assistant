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

    # The butler's model: the agent loop in core/llm.py. It transcribes
    # photographed handwriting, chooses tools and does date arithmetic, which is
    # the hardest thing this product does and the reason it exists. Keeps the
    # name `model` (env: MODEL) because production pins that variable — renaming
    # it would drop the pin silently and fall back to this default.
    model: str = "claude-opus-5"

    # The digest composer's model: jobs/digest.py. One short paragraph written
    # from a ~300-token JSON payload, no tools, no images, nothing to reason
    # about. Sharing one setting with the butler meant this ran at Opus rates
    # for a task that never used the capability, and neither could be tuned
    # without moving the other.
    #
    # If you drop this to claude-haiku-4-5, you must also remove the
    # `output_config={"effort": ...}` in compose_digest: Haiku 4.5 rejects the
    # effort parameter outright, where Sonnet 5 accepts it.
    digest_model: str = "claude-sonnet-5"

    # Deepgram, for transcribing voice notes. Empty by default so a checkout
    # with no voice feature configured still imports, and so the test suite
    # never holds a real credential; a transcription attempted without it fails
    # visibly at the vendor rather than quietly doing nothing.
    deepgram_api_key: str = ""

    debounce_seconds: float = 3.0


settings = Settings()  # type: ignore[call-arg]
