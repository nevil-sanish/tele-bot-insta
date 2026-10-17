"""Phase 0: run real reels through fetch → Gemini and measure.

    python -m scripts.phase0_test urls.txt          # one Instagram URL per line
    python -m scripts.phase0_test URL [URL ...]

Prints per-reel fetch source/time, analysis model/mode, token usage and the
title, then a summary. Writes full results to phase0_results.json.
Needs APIFY_TOKEN (and/or yt-dlp working) + GEMINI_API_KEY; GROQ_API_KEY for the backup.
"""

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fetchers import chain  # noqa: E402
from services import analyze  # noqa: E402


def read_urls(args: list[str]) -> list[str]:
    urls = []
    for a in args:
        p = Path(a)
        if p.is_file():
            urls += [line.strip() for line in p.read_text().splitlines() if line.strip().startswith("http")]
        else:
            urls.append(a)
    return urls


async def run_one(url: str) -> dict:
    row: dict = {"url": url}
    t0 = time.monotonic()
    try:
        fetched = await chain.fetch(url)
    except Exception as e:
        row.update(ok=False, stage="fetch", error=str(e), fetch_s=round(time.monotonic() - t0, 1))
        return row
    row.update(source=fetched.source, fetch_s=round(time.monotonic() - t0, 1),
               size_mb=round(sum(m.path.stat().st_size for m in fetched.media) / 1e6, 2),
               caption_chars=len(fetched.caption or ""))
    t1 = time.monotonic()
    try:
        result = await analyze.analyze(fetched.media, fetched.caption)
        row.update(ok=True, model=result.model, mode=result.mode, usage=result.usage,
                   analyze_s=round(time.monotonic() - t1, 1), output=result.data.model_dump())
    except Exception as e:
        row.update(ok=False, stage="analyze", error=str(e))
    finally:
        fetched.cleanup()
    return row


async def main() -> None:
    urls = read_urls(sys.argv[1:])
    if not urls:
        sys.exit(__doc__)
    rows = []
    for i, url in enumerate(urls, 1):
        row = await run_one(url)
        rows.append(row)
        if row["ok"]:
            u = row.get("usage") or {}
            print(f"[{i}] ✅ {row['source']} {row['fetch_s']}s | {row['model']} ({row['mode']}) "
                  f"{row['analyze_s']}s | in={u.get('input')} out={u.get('output')} | {row['output']['title']}")
        else:
            print(f"[{i}] ❌ {row['stage']}: {row['error'][:150]}")

    ok = [r for r in rows if r["ok"]]
    print(f"\nSuccess: {len(ok)}/{len(rows)}")
    sources = {}
    for r in rows:
        if r.get("source"):
            sources[r["source"]] = sources.get(r["source"], 0) + 1
    print("Fetched by:", sources or "nothing")
    ins = [r["usage"]["input"] for r in ok if (r.get("usage") or {}).get("input")]
    outs = [r["usage"]["output"] for r in ok if (r.get("usage") or {}).get("output")]
    if ins:
        print(f"Avg tokens/reel: input {sum(ins) // len(ins)}, output {sum(outs) // max(1, len(outs))}")
    Path("phase0_results.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False))
    print("Full results → phase0_results.json")
    print("\nNext: open AI Studio → Rate limits and note the RPD for each model in GEMINI_MODELS.")


if __name__ == "__main__":
    asyncio.run(main())
