import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Force fake values: tests must never pick up real keys from the environment.
os.environ["TELEGRAM_TOKEN"] = "123:test"
os.environ["WEBHOOK_SECRET"] = "s3cret"
os.environ["ALLOWED_USER_IDS"] = "42"
os.environ["TMP_DIR"] = "/tmp/reel-brain-tests"
for key in ("NOTION_TOKEN", "NOTION_DATABASE_ID", "APIFY_TOKEN", "COBALT_URL", "GEMINI_API_KEY", "GROQ_API_KEY"):
    os.environ.pop(key, None)
