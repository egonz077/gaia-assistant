import os

# MUST come before any gaia import: gaia.core.config instantiates Settings()
# at import time, by design, so a missing variable fails at startup.
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test")
os.environ.setdefault("VOYAGE_API_KEY", "pa-test")
os.environ.setdefault("WA_ACCESS_TOKEN", "test-token")
os.environ.setdefault("WA_APP_SECRET", "test-secret")
os.environ.setdefault("WA_VERIFY_TOKEN", "test-verify")
os.environ.setdefault("WA_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("DATABASE_URL", "postgresql://placeholder/overridden_by_pool_fixture")
