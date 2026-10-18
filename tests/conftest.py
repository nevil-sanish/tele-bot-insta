import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Force fake values: tests must never pick up real keys from the environment.
os.environ["INSTAGRAM_APP_SECRET"] = "app-s3cret"
os.environ["INSTAGRAM_VERIFY_TOKEN"] = "verify-me"
os.environ["INSTAGRAM_ACCESS_TOKEN"] = "ig-token"
os.environ["ADMIN_IG_IDS"] = "admin-1"
os.environ["TMP_DIR"] = "/tmp/reel-brain-tests"
# Placeholder so the app imports; tests that use a database get TEST_DATABASE_URL instead.
os.environ["DATABASE_URL"] = "postgresql://unused@localhost/unused"
for key in ("APP_SECRET", "GOOGLE_CLIENT_IDS", "APIFY_TOKEN", "APIFY_TOKENS", "GEMINI_API_KEY",
            "GEMINI_API_KEYS", "GROQ_API_KEY", "GROQ_API_KEYS", "FREE_UNITS", "UNITS_PER_REEL"):
    os.environ.pop(key, None)

import asyncpg  # noqa: E402
import pytest  # noqa: E402

# Database tests need a throwaway Postgres, e.g.
#   TEST_DATABASE_URL=postgresql://postgres@localhost:5432/reelbrain_test
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")


@pytest.fixture
async def pg():
    """Fresh, empty schema for each test, connected through services.db."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    from services import db

    con = await asyncpg.connect(TEST_DATABASE_URL)
    await con.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await con.close()
    await db.connect(TEST_DATABASE_URL)
    yield db
    await db.close()
