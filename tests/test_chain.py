import asyncio

import pytest

from fetchers import chain
from fetchers.base import FetchError, FetchResult


def make(name, behaviour, enabled=True, timeout=0.2):
    async def fetch(url):
        if behaviour == "ok":
            return FetchResult(source=name)
        if behaviour == "slow":
            await asyncio.sleep(5)
        raise FetchError(f"{name} broke")

    return chain.Fetcher(name, fetch, lambda: enabled, timeout)


async def test_first_success_wins():
    fs = [make("a", "fail"), make("b", "slow"), make("c", "ok"), make("d", "ok")]
    result = await chain.fetch("u", fs)
    assert result.source == "c"


async def test_disabled_fetchers_are_skipped():
    fs = [make("a", "ok", enabled=False), make("b", "ok")]
    assert (await chain.fetch("u", fs)).source == "b"


async def test_all_fail_lists_reasons():
    with pytest.raises(FetchError) as e:
        await chain.fetch("u", [make("a", "fail"), make("b", "slow")])
    assert "a: a broke" in str(e.value) and "b: timeout" in str(e.value)
