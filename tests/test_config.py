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
