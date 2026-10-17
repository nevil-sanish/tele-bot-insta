"""Keyword search over the library with BM25: local, in memory, zero tokens."""

import re
import threading

from rank_bm25 import BM25Okapi

from .notion_store import Reel

STOPWORDS = set(
    "a an and are as at be but by can do does for from how i in is it me my of on or so "
    "that the this to was what when where which who why will with you your about any all".split()
)


def tokenize(text: str) -> list[str]:
    return [t for t in re.findall(r"\w+", (text or "").lower()) if t not in STOPWORDS]


class SearchIndex:
    def __init__(self) -> None:
        self._reels: dict[str, Reel] = {}
        self._bm25: BM25Okapi | None = None
        self._order: list[str] = []
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._reels)

    def load(self, reels: list[Reel]) -> None:
        with self._lock:
            self._reels = {r.shortcode: r for r in reels if r.shortcode}
            self._rebuild()

    def add(self, reel: Reel) -> None:
        with self._lock:
            self._reels[reel.shortcode] = reel
            self._rebuild()

    def _rebuild(self) -> None:
        # Rebuilding is cheap (a few thousand short docs) and keeps IDF correct.
        self._order = list(self._reels)
        corpus = [tokenize(self._reels[k].search_text()) or ["_"] for k in self._order]
        self._bm25 = BM25Okapi(corpus) if corpus else None

    def search(self, query: str, k: int = 3) -> list[Reel]:
        q = tokenize(query)
        with self._lock:
            if not self._bm25 or not q:
                return []
            scores = self._bm25.get_scores(q)
            ranked = sorted(zip(scores, self._order), key=lambda x: x[0], reverse=True)
            # BM25 can give 0 or negative scores for terms in most docs; require a real match.
            hits = [code for score, code in ranked if score > 0][:k]
            if not hits:
                qs = set(q)
                hits = [c for _, c in ranked if qs & set(tokenize(self._reels[c].search_text()))][:k]
            return [self._reels[c] for c in hits]


index = SearchIndex()
