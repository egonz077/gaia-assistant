from gaia.core.config import Settings


def test_settings_read_from_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("VOYAGE_API_KEY", "pa-test")
    monkeypatch.setenv("WA_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("WA_APP_SECRET", "secret")
    monkeypatch.setenv("WA_VERIFY_TOKEN", "verify")
    monkeypatch.setenv("WA_PHONE_NUMBER_ID", "123")

    s = Settings(_env_file=None)

    assert s.database_url == "postgresql://u:p@localhost/db"
    assert s.model == "claude-opus-5"       # default, _env_file=None
    assert s.debounce_seconds == 3.0        # default


def test_the_butler_and_the_digest_have_separate_models(monkeypatch):
    """Two workloads with nothing in common: a multi-turn agent loop that reads
    photographed handwriting and picks tools, and a single-shot composer turning
    a small JSON payload into one warm paragraph. One setting for both meant
    neither could be tuned without moving the other."""
    for var in ("DATABASE_URL", "ANTHROPIC_API_KEY", "VOYAGE_API_KEY", "WA_ACCESS_TOKEN",
                "WA_APP_SECRET", "WA_VERIFY_TOKEN", "WA_PHONE_NUMBER_ID"):
        monkeypatch.setenv(var, "x")

    s = Settings(_env_file=None)

    assert s.model == "claude-opus-5", "the butler reads handwriting; it stays on Opus"
    assert s.digest_model == "claude-sonnet-5"


def test_each_model_is_settable_on_its_own(monkeypatch):
    """MODEL is pinned in the production .env. Renaming it would have dropped
    that pin silently, so it keeps its name and DIGEST_MODEL joins it."""
    for var in ("DATABASE_URL", "ANTHROPIC_API_KEY", "VOYAGE_API_KEY", "WA_ACCESS_TOKEN",
                "WA_APP_SECRET", "WA_VERIFY_TOKEN", "WA_PHONE_NUMBER_ID"):
        monkeypatch.setenv(var, "x")
    monkeypatch.setenv("MODEL", "claude-opus-5")
    monkeypatch.setenv("DIGEST_MODEL", "claude-haiku-4-5")

    s = Settings(_env_file=None)

    assert s.model == "claude-opus-5"
    assert s.digest_model == "claude-haiku-4-5"
