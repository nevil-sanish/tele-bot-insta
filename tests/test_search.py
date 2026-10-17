from services.notion_store import Reel
from services.search import SearchIndex


def test_bm25_ranks_relevant_reels():
    idx = SearchIndex()
    idx.load([
        Reel(shortcode="1", title="Python tips", summary="Use list comprehensions", tags=["python"]),
        Reel(shortcode="2", title="Leg day", summary="Squats and lunges", tags=["fitness"]),
        Reel(shortcode="3", title="Budgeting", summary="Track your spending monthly", tags=["finance"]),
    ])
    assert [r.shortcode for r in idx.search("python comprehensions")][:1] == ["1"]
    assert [r.shortcode for r in idx.search("squats")] == ["2"]
    assert idx.search("quantum") == []
    idx.add(Reel(shortcode="4", title="Quantum computing intro", summary="qubits"))
    assert [r.shortcode for r in idx.search("quantum")] == ["4"]


def test_empty_index():
    assert SearchIndex().search("anything") == []
