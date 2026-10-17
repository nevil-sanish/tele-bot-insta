"""One-time setup: add the Reel Brain properties to your Notion database.

    python -m scripts.setup_notion

Needs NOTION_TOKEN and NOTION_DATABASE_ID, and the integration must be
connected to the database (database ⋯ menu → Connections).
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from notion_client import AsyncClient  # noqa: E402


def select(*names):
    return {"select": {"options": [{"name": n} for n in names]}}


PROPERTIES = {
    "Shortcode": {"rich_text": {}},
    "Link": {"url": {}},
    "Author": {"rich_text": {}},
    "Summary": {"rich_text": {}},
    "Tags": {"multi_select": {}},
    "Category": {"select": {}},
    "Analysis": select("full", "audio-only", "caption-only"),
    "Source": select("apify", "yt-dlp", "cobalt", "forwarded"),
    "Status": select("done", "failed"),
    "Date": {"date": {}},
}


async def main() -> None:
    client = AsyncClient(auth=os.environ["NOTION_TOKEN"])
    db = await client.databases.retrieve(database_id=os.environ["NOTION_DATABASE_ID"])
    ds_id = db["data_sources"][0]["id"]
    ds = await client.data_sources.retrieve(ds_id)
    existing = ds["properties"]

    update: dict = {}
    title_name = next(name for name, p in existing.items() if p["type"] == "title")
    if title_name != "Title":
        update[title_name] = {"name": "Title"}
    for name, spec in PROPERTIES.items():
        if name not in existing:
            update[name] = spec
        elif existing[name]["type"] != next(iter(spec)):
            print(f"⚠️  '{name}' exists with type {existing[name]['type']}, expected {next(iter(spec))}; fix it by hand")

    if update:
        await client.data_sources.update(ds_id, properties=update)
        print("Added/renamed:", ", ".join(update))
    else:
        print("Database already has every property.")


if __name__ == "__main__":
    asyncio.run(main())
