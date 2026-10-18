from services.notion_store import Reel, build_blocks, build_properties, reel_from_page


def _as_api(blocks):
    """Notion returns plain_text on rich text objects; mimic that."""
    out = []
    for b in blocks:
        t = b["type"]
        rich = [{"plain_text": r["text"]["content"]} for r in b[t]["rich_text"]]
        out.append({"type": t, t: {"rich_text": rich}})
    return out


def _page_from_props(props):
    api = {}
    for name, p in props.items():
        if "title" in p:
            api[name] = {"type": "title", "title": [{"plain_text": r["text"]["content"]} for r in p["title"]]}
        elif "rich_text" in p:
            api[name] = {"type": "rich_text", "rich_text": [{"plain_text": r["text"]["content"]} for r in p["rich_text"]]}
        elif "url" in p:
            api[name] = {"type": "url", "url": p["url"]}
        elif "select" in p:
            api[name] = {"type": "select", "select": p["select"]}
        elif "multi_select" in p:
            api[name] = {"type": "multi_select", "multi_select": p["multi_select"]}
        elif "date" in p:
            api[name] = {"type": "date", "date": p["date"]}
    return {"id": "page1", "url": "https://notion.so/page1", "properties": api}


def test_roundtrip_properties_and_body():
    reel = Reel(
        shortcode="ABC", link="https://www.instagram.com/reel/ABC/", title="Title", summary="Sum",
        key_ideas=["one", "two"], tools=["Python"], urls=["https://x.io"], tags=["ai", "a,b"],
        category="tech", on_screen_text="SALE", transcript="t" * 4500, caption="cap #x",
        author="me", source="apify", analysis="full",
    )
    page = _page_from_props(build_properties(reel))
    back = reel_from_page(page, _as_api(build_blocks(reel)))
    assert back.shortcode == "ABC" and back.title == "Title" and back.link == reel.link
    assert back.tags == ["ai", "a b"]  # commas aren't allowed in select options
    assert back.key_ideas == ["one", "two"] and back.tools == ["Python"] and back.urls == ["https://x.io"]
    assert back.transcript == reel.transcript  # long text split across blocks and joined back
    assert back.caption == "cap #x" and back.on_screen_text == "SALE"
    assert back.notion_url == "https://notion.so/page1" and back.status == "done"


def test_long_text_is_chunked_under_notion_limit():
    blocks = build_blocks(Reel(shortcode="x", transcript="y" * 5000))
    for b in blocks:
        for r in b[b["type"]]["rich_text"]:
            assert len(r["text"]["content"]) <= 2000


def test_failed_properties_carry_reason():
    props = build_properties(Reel(shortcode="x", status="failed"), error="Failed: boom")
    assert props["Status"]["select"]["name"] == "failed"
    assert props["Summary"]["rich_text"][0]["text"]["content"] == "Failed: boom"
    assert "Link" not in props


def test_public_link_preferred_when_published():
    from services.notion_store import page_link

    assert page_link({"url": "https://www.notion.so/x", "public_url": "https://me.notion.site/x"}) == "https://me.notion.site/x"
    assert page_link({"url": "https://www.notion.so/x", "public_url": None}) == "https://www.notion.so/x"
