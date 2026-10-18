"""P0 spike: what does Apify cost per reel when reels are batched into one run?

    python -m scripts.spike_apify natgeo 20

1. Lists recent reels of a public profile (one small run).
2. Fetches all of them in ONE batched run, like the production fetch stage.
3. Prints success rate, run time and USD per reel (Apify's own usage numbers).
Needs APIFY_TOKEN (or APIFY_TOKENS) in the environment.
"""

import asyncio
import sys
import time

import httpx

from app import config

BASE = "https://api.apify.com/v2"


async def run_actor(client: httpx.AsyncClient, token: str, run_input: dict) -> tuple[dict, list[dict]]:
    headers = {"Authorization": f"Bearer {token}"}
    r = await client.post(
        f"{BASE}/acts/{config.APIFY_ACTOR}/runs", params={"waitForFinish": 240}, headers=headers, json=run_input
    )
    r.raise_for_status()
    run = r.json()["data"]
    while run["status"] in ("READY", "RUNNING"):
        await asyncio.sleep(5)
        run = (await client.get(f"{BASE}/actor-runs/{run['id']}", headers=headers)).json()["data"]
    items = (await client.get(f"{BASE}/datasets/{run['defaultDatasetId']}/items", headers=headers)).json()
    return run, items


def describe(label: str, run: dict, n: int) -> None:
    usd = run.get("usageTotalUsd") or 0
    secs = (run.get("stats") or {}).get("runTimeSecs") or 0
    print(f"{label}: status={run['status']} time={secs:.0f}s usd={usd:.4f} per_item={usd / max(n, 1):.5f}")


async def main(profile: str, count: int) -> None:
    token = config.APIFY_TOKENS[0]
    async with httpx.AsyncClient(timeout=300) as client:
        run, items = await run_actor(client, token, {
            "directUrls": [f"https://www.instagram.com/{profile}/reels/"],
            "resultsType": "posts", "resultsLimit": count, "addParentData": False,
        })
        urls = [i["url"] for i in items if isinstance(i, dict) and i.get("url") and i.get("videoUrl")][:count]
        describe("list", run, len(items))
        if not urls:
            print("no reel URLs found", items[:1])
            return

        t = time.monotonic()
        run, items = await run_actor(client, token, {
            "directUrls": urls, "resultsType": "posts", "resultsLimit": 1, "addParentData": False,
        })
        ok = [i for i in items if isinstance(i, dict) and i.get("videoUrl")]
        describe("batch", run, len(urls))
        print(f"batch: {len(ok)}/{len(urls)} reels with a video URL, wall {time.monotonic() - t:.0f}s")

        # Is the video URL actually downloadable from a datacenter IP?
        if ok:
            head = await client.get(ok[0]["videoUrl"], headers={"Range": "bytes=0-1023"})
            print(f"download check: HTTP {head.status_code}, {head.headers.get('content-type')}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "natgeo", int(sys.argv[2]) if len(sys.argv) > 2 else 20))
