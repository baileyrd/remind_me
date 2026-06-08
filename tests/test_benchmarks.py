"""
Tests for the benchmarks package — metrics, ingest strategies, the LongMemEval
loader, and an end-to-end synthetic retrieval run against the real search stack.

These tests are fully offline: the end-to-end run uses ``embedder="none"`` so it
exercises FTS5 keyword search with no model download.
"""

from __future__ import annotations

import json

import pytest

from benchmarks import download_data
from benchmarks import ingest as ingest_mod
from benchmarks import metrics as metrics_mod
from benchmarks.harness import Harness
from benchmarks.longmemeval import LongMemEvalItem, Turn, load_dataset, parse_item
from benchmarks.runner import _run_mode
from benchmarks.synthetic import make_dataset, make_item

# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------


def test_dedup_preserve_order():
    assert metrics_mod.dedup_preserve_order(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]


def test_first_relevant_rank():
    assert metrics_mod.first_relevant_rank(["x", "y", "z"], {"z"}) == 3
    assert metrics_mod.first_relevant_rank(["x", "y"], {"z"}) is None


@pytest.mark.parametrize(
    "ranked,relevant,k,expected",
    [
        (["a", "b", "c"], {"a"}, 1, 1.0),
        (["a", "b", "c"], {"c"}, 1, 0.0),
        (["a", "b", "c"], {"c"}, 3, 1.0),
        (["a", "b", "c"], set(), 5, 0.0),
    ],
)
def test_recall_at_k(ranked, relevant, k, expected):
    assert metrics_mod.recall_at_k(ranked, relevant, k) == expected


def test_reciprocal_rank():
    assert metrics_mod.reciprocal_rank(["a", "b", "c"], {"b"}) == pytest.approx(0.5)
    assert metrics_mod.reciprocal_rank(["a"], {"z"}) == 0.0


def test_aggregate_and_table():
    results = [
        metrics_mod.QueryResult("q1", "typeA", ["s1", "s2"], {"s1"}, 2),
        metrics_mod.QueryResult("q2", "typeA", ["s3", "s2"], {"s2"}, 2),
        metrics_mod.QueryResult("q3", "typeB", ["s9"], {"s1"}, 1),
    ]
    buckets = metrics_mod.aggregate(results, [1, 2])
    assert buckets["overall"].count == 3
    # typeA: q1 R@1=1, q2 R@1=0 -> mean 0.5 ; R@2 both 1 -> 1.0
    assert buckets["typeA"].recall(1) == pytest.approx(0.5)
    assert buckets["typeA"].recall(2) == pytest.approx(1.0)
    assert buckets["typeB"].recall(2) == 0.0

    table = metrics_mod.format_markdown_table({"verbatim": buckets}, [1, 2])
    assert "R@1" in table and "R@2" in table
    assert "verbatim" in table
    # overall row is rendered first within a mode group
    assert table.index("overall") < table.index("typeA")


# ---------------------------------------------------------------------------
# ingest strategies
# ---------------------------------------------------------------------------


def _two_session_item() -> LongMemEvalItem:
    return LongMemEvalItem(
        question_id="x",
        question_type="t",
        question="q",
        answer="a",
        sessions=[
            [Turn("user", "Hello there. How are you?"), Turn("assistant", "I am fine.")],
            [Turn("user", "Bye.")],
        ],
        session_ids=["s0", "s1"],
        answer_session_ids={"s0"},
    )


def test_ingest_verbatim_one_per_session():
    units = ingest_mod.ingest_verbatim(_two_session_item())
    assert len(units) == 2
    assert {u.session_id for u in units} == {"s0", "s1"}
    assert "Hello there" in units[0].content and "I am fine" in units[0].content


def test_ingest_turns_one_per_turn():
    units = ingest_mod.ingest_turns(_two_session_item())
    assert len(units) == 3
    assert units[0].session_id == "s0"
    assert units[-1].session_id == "s1"


def test_ingest_atomic_splits_sentences():
    units = ingest_mod.ingest_atomic(_two_session_item())
    # "Hello there." + "How are you?" + "I am fine." + "Bye." = 4 sentences
    assert len(units) == 4
    assert all(u.content for u in units)


def test_split_sentences():
    assert ingest_mod.split_sentences("One. Two! Three?") == ["One.", "Two!", "Three?"]
    assert ingest_mod.split_sentences("   ") == []


def test_get_strategy_unknown():
    with pytest.raises(ValueError, match="Unknown ingest mode"):
        ingest_mod.get_strategy("nope")


# ---------------------------------------------------------------------------
# LongMemEval loader
# ---------------------------------------------------------------------------


def test_parse_item_with_aliases():
    raw = {
        "id": "qid1",
        "type": "single-session-user",
        "question": "what?",
        "answer": "because",
        "sessions": [[{"speaker": "user", "text": "hi"}]],
        "session_ids": ["s0"],
        "evidence_session_ids": ["s0"],
    }
    item = parse_item(raw)
    assert item.question_id == "qid1"
    assert item.question_type == "single-session-user"
    assert item.sessions[0][0].role == "user"
    assert item.sessions[0][0].content == "hi"
    assert item.answer_session_ids == {"s0"}


def test_parse_item_content_parts_array():
    raw = {
        "question_id": "q",
        "haystack_sessions": [[{"role": "assistant", "content": [{"type": "text", "text": "part"}]}]],
        "haystack_session_ids": ["s0"],
        "answer_session_ids": [],
    }
    item = parse_item(raw)
    assert item.sessions[0][0].content == "part"


def test_load_dataset_roundtrip(tmp_path):
    data = [
        {
            "question_id": "a_abs",
            "question_type": "single-session-user",
            "question": "q",
            "answer": "a",
            "haystack_sessions": [[{"role": "user", "content": "hi"}]],
            "haystack_session_ids": ["s0"],
            "answer_session_ids": [],
        }
    ]
    p = tmp_path / "data.json"
    p.write_text(json.dumps(data))
    items = load_dataset(p)
    assert len(items) == 1
    assert items[0].is_abstention is True


# ---------------------------------------------------------------------------
# synthetic dataset
# ---------------------------------------------------------------------------


def test_make_item_evidence_token_unique():
    item = make_item(3, n_sessions=6)
    token = "zebracode3"
    evidence_sid = next(iter(item.answer_session_ids))
    # The token appears in the evidence session and nowhere else.
    for sid, turns in item.iter_sessions():
        joined = " ".join(t.content for t in turns)
        if sid == evidence_sid:
            assert token in joined
        else:
            assert token not in joined


def test_make_dataset_includes_abstention():
    items = make_dataset(5)
    assert len(items) == 6  # 5 answerable + 1 abstention
    assert any(it.is_abstention for it in items)


# ---------------------------------------------------------------------------
# download helper (offline pieces only)
# ---------------------------------------------------------------------------


def test_download_datasets_registry():
    assert set(download_data.DATASETS) == {"oracle", "s", "m"}
    assert download_data.DATASETS["oracle"] == "longmemeval_oracle.json"


def test_download_human_readable_sizes():
    assert download_data._human(512) == "512.0B"
    assert download_data._human(1536) == "1.5KB"
    assert download_data._human(5 * 1024 * 1024) == "5.0MB"


def test_download_validate_json(tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps([{"question_id": "a"}, {"question_id": "b"}]))
    assert download_data._validate_json(good) == 2

    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text(json.dumps({"data": [{"question_id": "a"}]}))
    assert download_data._validate_json(wrapped) == 1

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"nope": 1}))
    with pytest.raises(ValueError, match="not a JSON list"):
        download_data._validate_json(bad)


def test_download_fetch_skips_existing(tmp_path, capsys):
    existing = tmp_path / download_data.DATASETS["oracle"]
    existing.write_text("[]")
    # force=False and file present -> returns without any network call.
    result = download_data.fetch("oracle", tmp_path, force=False)
    assert result == existing


# ---------------------------------------------------------------------------
# harness end-to-end (offline, FTS-only)
# ---------------------------------------------------------------------------


def test_harness_ingest_and_search_fts():
    with Harness(embedder_mode="none") as h:
        h.reset()
        sid = "sess-A"
        h.ingest("The internal codename quetzalcoatl maps to project 7.", sid)
        h.ingest("Unrelated chatter about lunch options.", "sess-B")
        memories = h.search_sync("quetzalcoatl", limit=10)
        assert memories, "expected at least one keyword hit"
        assert memories[0]["metadata"]["session_id"] == sid


async def test_before_after_sample_shows_improvement():
    """On the bundled punctuated sample, sanitization lifts keyword-only recall."""
    from pathlib import Path

    from benchmarks.before_after import _score
    from benchmarks.longmemeval import load_dataset

    sample = Path(__file__).resolve().parents[1] / "benchmarks" / "sample" / "longmemeval_sample.json"
    items = load_dataset(sample)
    ks = [1, 3, 5]

    before = await _score(items, "verbatim", "none", ks, 100, "sanitize", "before")
    after = await _score(items, "verbatim", "none", ks, 100, "sanitize", "after")

    assert before["overall"].recall(3) == 0.0  # punctuated NL queries can't match raw FTS5
    assert after["overall"].recall(3) > 0.9    # sanitized OR-of-terms recovers them


async def test_end_to_end_synthetic_recall_perfect():
    """FTS-only retrieval on the synthetic set is deterministic Recall@1 == 1.0."""
    items = make_dataset(6)
    results = await _run_mode(
        items,
        mode="verbatim",
        embedder="none",
        ks=[1, 3, 5],
        limit=50,
        skip_abstention=True,
        progress=False,
    )
    buckets = metrics_mod.aggregate(results, [1, 3, 5])
    assert buckets["overall"].count == 6  # abstention skipped
    assert buckets["overall"].recall(1) == 1.0
    assert buckets["overall"].mrr == 1.0
