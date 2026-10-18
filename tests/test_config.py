from app import config


def test_str_strips_spaces_and_quotes(monkeypatch):
    monkeypatch.setenv("X_TEST", '  "abc:123"  ')
    assert config._str("X_TEST") == "abc:123"
    monkeypatch.setenv("X_TEST", "   ")
    assert config._str("X_TEST", "d") == ""


def test_missing_required_lists_names_only(monkeypatch):
    for name in config.REQUIRED:
        monkeypatch.setenv(name, "v")
    monkeypatch.setenv("GROQ_API_KEY", " ")
    monkeypatch.delenv("NOTION_TOKEN")
    assert config.missing_required() == ["GROQ_API_KEY", "NOTION_TOKEN"]
