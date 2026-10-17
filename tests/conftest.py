import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("TELEGRAM_TOKEN", "123:test")
os.environ.setdefault("WEBHOOK_SECRET", "s3cret")
os.environ.setdefault("ALLOWED_USER_IDS", "42")
os.environ.setdefault("TMP_DIR", "/tmp/reel-brain-tests")
for key in ("NOTION_TOKEN", "NOTION_DATABASE_ID", "APIFY_TOKEN", "COBALT_URL", "GEMINI_API_KEY", "GROQ_API_KEY"):
    os.environ.pop(key, None)
