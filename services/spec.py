"""The shared analysis spec (spec/analysis.json): prompt, output fields, video
settings and chat prompts. The Android app bundles an exact copy, so the bot
and the app produce the same results."""

import json
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / "spec" / "analysis.json"
SPEC: dict = json.loads(PATH.read_text(encoding="utf-8"))

FIELDS: list[str] = [f["name"] for f in SPEC["fields"]]
LIST_FIELDS: set[str] = {f["name"] for f in SPEC["fields"] if f["type"] == "list"}
GEMINI: dict = SPEC["gemini"]
CHAT: dict = SPEC["chat"]
