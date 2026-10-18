"""Notion is both the library and the cache (keyed by Shortcode).

Uses Notion API 2025-09-03 (notion-client 3.x): pages live in a *data source*
inside the database, so we resolve the database's first data source once.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app import config

log = logging.getLogger(__name__)

TEXT_LIMIT = 1900  # Notion caps a rich-text object at 2000 chars
BLOCK_BATCH = 100  # and a request at 100 child blocks

# Page-body sections, in order. (heading, Reel attribute, is_list)
SECTIONS = [
    ("Key ideas", "key_ideas", True),
    ("Tools", "tools", True),
    ("Links", "urls", True),
    ("On-screen text", "on_screen_text", False),
    ("Transcript", "transcript", False),
    ("Caption", "caption", False),
]


@dataclass
class Reel:
    """Everything we keep about one reel (text only)."""

    shortcode: str
    link: str = ""
    title: str = ""
    summary: str = ""
    key_ideas: list[str] = field(default_factory=list)
    on_screen_text: str = ""
    transcript: str = ""
    tools: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    category: str = ""
    caption: str = ""
    hashtags: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    author: str = ""
    date: str | None = None
    analysis: str = "full"
    source: str = ""
    status: str = "done"
    page_id: str = ""
    notion_url: str = ""

    def search_text(self) -> str:
        return " ".join(
            [self.title, self.summary, " ".join(self.key_ideas), " ".join(self.tools),
             " ".join(self.tags), " ".join(self.hashtags), self.category, self.author]
        )


# ---------------------------------------------------------------- property helpers


def _text(s: str) -> list[dict]:
    s = s or ""
    return [{"type": "text", "text": {"content": s[i:i + TEXT_LIMIT]}} for i in range(0, len(s), TEXT_LIMIT)][:100]


def _option(s: str) -> str:
    # Select options can't contain commas and are limited to 100 chars.
    return (s or "").replace(",", " ").strip()[:100]


def _plain(rich: list[dict] | None) -> str:
    return "".join(r.get("plain_text") or r.get("text", {}).get("content", "") for r in rich or [])


def _prop(props: dict, name: str):
    p = props.get(name) or {}
    t = p.get("type")
    if t in ("title", "rich_text"):
        return _plain(p.get(t))
    if t == "url":
        return p.get("url") or ""
    if t == "select":
        return (p.get("select") or {}).get("name", "")
    if t == "multi_select":
        return [o["name"] for o in p.get("multi_select") or []]
    if t == "date":
        return (p.get("date") or {}).get("start")
    return None


def page_link(page: dict) -> str:
    """Public read-only link once the database is published to the web, else the private one."""
    return page.get("public_url") or page.get("url") or ""


def build_properties(reel: Reel, error: str = "") -> dict:
    tags = list(dict.fromkeys(_option(t) for t in reel.tags if _option(t)))[:10]
    props = {
        "Title": {"title": _text(reel.title or reel.shortcode)},
        "Shortcode": {"rich_text": _text(reel.shortcode)},
        "Author": {"rich_text": _text(reel.author)},
        "Summary": {"rich_text": _text(error or reel.summary)},
        "Tags": {"multi_select": [{"name": t} for t in tags]},
        "Status": {"select": {"name": reel.status}},
        "Date": {"date": {"start": reel.date or datetime.now(timezone.utc).isoformat()}},
    }
    if reel.link:
        props["Link"] = {"url": reel.link}
    for name, value in (("Category", reel.category), ("Analysis", reel.analysis), ("Source", reel.source)):
        if _option(value):
            props[name] = {"select": {"name": _option(value)}}
    return props


def build_blocks(reel: Reel) -> list[dict]:
    blocks: list[dict] = []
    for heading, attr, is_list in SECTIONS:
        value = getattr(reel, attr)
        if not value:
            continue
        blocks.append({"type": "heading_2", "heading_2": {"rich_text": _text(heading)}})
        if is_list:
            for item in value:
                blocks.append({"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": _text(item)}})
        else:
            for i in range(0, len(value), TEXT_LIMIT):
                blocks.append({"type": "paragraph", "paragraph": {"rich_text": _text(value[i:i + TEXT_LIMIT])}})
    return blocks


def reel_from_page(page: dict, blocks: list[dict] | None = None) -> Reel:
    props = page.get("properties", {})
    reel = Reel(
        shortcode=_prop(props, "Shortcode") or "",
        link=_prop(props, "Link") or "",
        title=_prop(props, "Title") or "",
        summary=_prop(props, "Summary") or "",
        tags=_prop(props, "Tags") or [],
        category=_prop(props, "Category") or "",
        author=_prop(props, "Author") or "",
        date=_prop(props, "Date"),
        analysis=_prop(props, "Analysis") or "",
        source=_prop(props, "Source") or "",
        status=_prop(props, "Status") or "",
        page_id=page.get("id", ""),
        notion_url=page_link(page),
    )
    if blocks:
        by_heading = {h: (attr, is_list) for h, attr, is_list in SECTIONS}
        current = None
        for b in blocks:
            t = b.get("type")
            text = _plain((b.get(t) or {}).get("rich_text"))
            if t == "heading_2":
                current = by_heading.get(text)
            elif current:
                attr, is_list = current
                if is_list:
                    getattr(reel, attr).append(text)
                else:
                    setattr(reel, attr, getattr(reel, attr) + text)
    return reel


# ---------------------------------------------------------------- store


class NotionStore:
    def __init__(self, token: str | None = None, database_id: str | None = None):
        from notion_client import AsyncClient

        self.client = AsyncClient(auth=token or config.NOTION_TOKEN)
        self.database_id = database_id or config.NOTION_DATABASE_ID
        self._data_source_id: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(config.NOTION_TOKEN and self.database_id)

    async def data_source_id(self) -> str:
        if not self._data_source_id:
            db = await self.client.databases.retrieve(database_id=self.database_id)
            sources = db.get("data_sources") or []
            # Older/odd setups: the ID given may already be a data source ID.
            self._data_source_id = sources[0]["id"] if sources else self.database_id
        return self._data_source_id

    async def _query(self, **kwargs) -> list[dict]:
        ds = await self.data_source_id()
        results, cursor = [], None
        limit = kwargs.pop("limit", None)
        while True:
            body = dict(kwargs, page_size=100)
            if cursor:
                body["start_cursor"] = cursor
            resp = await self.client.data_sources.query(ds, **body)
            results += [r for r in resp.get("results", []) if r.get("object") == "page"]
            cursor = resp.get("next_cursor")
            if not resp.get("has_more") or not cursor or (limit and len(results) >= limit):
                return results[:limit] if limit else results

    async def _blocks(self, page_id: str) -> list[dict]:
        out, cursor = [], None
        while True:
            kw = {"block_id": page_id, "page_size": 100}
            if cursor:
                kw["start_cursor"] = cursor
            resp = await self.client.blocks.children.list(**kw)
            out += resp.get("results", [])
            cursor = resp.get("next_cursor")
            if not resp.get("has_more") or not cursor:
                return out

    async def find(self, shortcode: str, status: str | None = "done") -> Reel | None:
        """Cache lookup. Returns the full reel (properties + body) or None."""
        flt: dict = {"property": "Shortcode", "rich_text": {"equals": shortcode}}
        if status:
            flt = {"and": [flt, {"property": "Status", "select": {"equals": status}}]}
        pages = await self._query(filter=flt, limit=1)
        if not pages:
            return None
        return reel_from_page(pages[0], await self._blocks(pages[0]["id"]))

    async def _trash_existing(self, shortcode: str) -> None:
        """Remove older pages for this shortcode (e.g. a failed attempt)."""
        flt = {"property": "Shortcode", "rich_text": {"equals": shortcode}}
        for page in await self._query(filter=flt):
            await self.client.pages.update(page_id=page["id"], in_trash=True)

    async def save(self, reel: Reel, error: str = "") -> Reel:
        await self._trash_existing(reel.shortcode)
        blocks = [] if error else build_blocks(reel)
        page = await self.client.pages.create(
            parent={"type": "data_source_id", "data_source_id": await self.data_source_id()},
            properties=build_properties(reel, error),
            children=blocks[:BLOCK_BATCH],
        )
        for i in range(BLOCK_BATCH, len(blocks), BLOCK_BATCH):
            await self.client.blocks.children.append(block_id=page["id"], children=blocks[i:i + BLOCK_BATCH])
        reel.page_id, reel.notion_url = page["id"], page_link(page)
        return reel

    async def save_failed(self, reel: Reel, reason: str) -> None:
        reel.status = "failed"
        await self.save(reel, error=f"Failed: {reason}"[:1900])

    async def list_recent(self, n: int = 10) -> list[Reel]:
        pages = await self._query(
            filter={"property": "Status", "select": {"equals": "done"}},
            sorts=[{"timestamp": "created_time", "direction": "descending"}],
            limit=n,
        )
        return [reel_from_page(p) for p in pages]

    async def list_failed(self, n: int = 10) -> list[Reel]:
        pages = await self._query(
            filter={"property": "Status", "select": {"equals": "failed"}},
            sorts=[{"timestamp": "created_time", "direction": "descending"}],
            limit=n,
        )
        return [reel_from_page(p) for p in pages]

    async def load_all(self) -> list[Reel]:
        """Properties only (no bodies): enough for the BM25 index."""
        pages = await self._query(filter={"property": "Status", "select": {"equals": "done"}})
        return [reel_from_page(p) for p in pages]
