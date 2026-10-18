import json
from pathlib import Path

from services import analyze, spec

ROOT = Path(__file__).resolve().parent.parent


def test_python_model_matches_spec_fields():
    assert list(analyze.ReelAnalysis.model_fields) == spec.FIELDS
    assert analyze.RESPONSE_SCHEMA["required"] == spec.FIELDS


def test_mobile_app_bundles_the_same_spec():
    """The Android app ships its own copy; it must never drift from the bot's."""
    mobile = ROOT / "mobile" / "assets" / "analysis_spec.json"
    assert json.loads(mobile.read_text()) == spec.SPEC
