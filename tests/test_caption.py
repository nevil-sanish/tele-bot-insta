from services import caption as cap


def test_shortcode_from_various_urls():
    assert cap.extract_shortcode("https://www.instagram.com/reel/C1a2B3c4D5e/?igsh=abc") == "C1a2B3c4D5e"
    assert cap.extract_shortcode("look https://instagram.com/p/Xy_Z-123/ nice") == "Xy_Z-123"
    assert cap.extract_shortcode("https://www.instagram.com/someuser/reel/ABCdef123/") == "ABCdef123"
    assert cap.extract_shortcode("https://www.instagram.com/reels/QWErty/") == "QWErty"
    assert cap.extract_shortcode("https://youtube.com/watch?v=1") is None
    assert cap.extract_shortcode(None) is None


def test_find_ig_url():
    assert cap.find_ig_url("check https://www.instagram.com/reel/AAA111/ ok") == "https://www.instagram.com/reel/AAA111/"
    assert cap.find_ig_url("no link") is None


def test_hashtags_urls_mentions():
    text = "Try this #Python tip! #ai #AI by @dev.guy see https://example.com/x, and http://a.io."
    assert cap.extract_hashtags(text) == ["Python", "ai"]
    assert cap.extract_urls(text) == ["https://example.com/x", "http://a.io"]
    assert cap.extract_mentions(text) == ["dev.guy"]
    assert cap.extract_hashtags("email me a#b") == []
