"""TREC/MS MARCO eval helpers; no model download."""
from pathlib import Path

from src.core.trec_qrels import parse_qrels, parse_queries, trec_coverage
from src.core.retrieval_metrics import binary_metrics
import pytest


def test_metrics_use_all_gold_and_skip_unjudged():
    assert binary_metrics(["a"], {"a", "b", "c", "d"})["recall_at_5"] == 0.25
    assert binary_metrics(["a", "a"], {"a", "b"})["recall_at_5"] == 0.5
    assert binary_metrics(["a"], {"a", "b"})["ndcg_at_10"] < 1
    assert binary_metrics(["a"], set()) is None


def test_small_corpus_target_cannot_drop_gold():
    import random
    from scripts.eval_retrieval_datasets import build_corpus
    rows = [{"query": "q", "passages": {"passage_text": ["noise", "gold"], "is_selected": [0, 1]}},
            {"query": "unjudged", "passages": {"passage_text": ["other"]}}]
    corpus, positives, stats = build_corpus(rows, [0, 1], 1, random.Random(42))
    assert "gold" in corpus and positives == {0: {"gold"}}
    assert stats["skipped_no_positive"] == 1 and stats["corpus_target_exceeded"]


def test_pid_alignment_does_not_match_text_or_qa_ids(tmp_path):
    from src.core.trec_qrels import audit_passage_collection
    path = tmp_path / "collection.tsv"
    path.write_text("42\tcorrect passage\n99\twrong document even if text matches\ninvalid\n", encoding="utf-8")
    result = audit_passage_collection(path, {"42", "88"})
    assert result["n_found"] == 1 and result["missing_pid_examples"] == ["88"]
    assert result["malformed_rows"] == 1


@pytest.mark.parametrize("mode", ["auto", "semantic"])
@pytest.mark.parametrize("kind", [None, "knowledge"])
def test_semantic_recall_keeps_paraphrase_and_trusted_filters(monkeypatch, mode, kind):
    from src.common.models import RetrievalQuery, MemoryType
    from src.core.orchestrator import MemoryOrchestrator
    from src.knowledge.memory_service.in_memory_client import InMemoryMilvusClient
    from src.knowledge.memory_service.embedding_client import EmbeddingClient
    monkeypatch.setattr(EmbeddingClient, "embed", lambda self, text: [1.0, 0.0])
    store = InMemoryMilvusClient()
    for mid, user, latest in [("target", "u", True), ("other", "v", True), ("old", "u", False)]:
        store.insert(mid, "Cardiovascular fitness improves with regular exercise.", [1.0, 0.0],
                     {"user_id": user, "is_latest": latest, "memory_type": "document_chunk"})
    original_search = store.search
    calls = []
    def search(embedding, filters, top_k=5):
        calls.append(embedding)
        assert embedding is not None, "Open-domain queries must not scan the lexical pool"
        return original_search(embedding, filters, top_k)
    monkeypatch.setattr(store, "search", search)
    query = RetrievalQuery(query="health benefits of jogging", user_id="u", memory_type=kind,
                           retrieval_mode=mode)
    result = MemoryOrchestrator(milvus=store).recall(query, filters={"user_id": "v"})
    assert [i.id for i in result.items] == ["target"]
    assert result.strategy == ("query_memory:auto-semantic" if mode == "auto" else "query_memory:semantic")
    assert len(calls) == 1


def test_parse_queries_and_qrels(tmp_path):
    (tmp_path / "trec_dl_2019_queries.tsv").write_text("19335\twhat is a triangle\n", encoding="utf-8")
    (tmp_path / "trec_dl_2020_queries.tsv").write_text("", encoding="utf-8")
    (tmp_path / "2019qrels-pass.txt").write_text(
        "19335 Q0 1017759 0\n19335 Q0 42 3\n19335 Q0 99 1\n",
        encoding="utf-8",
    )
    (tmp_path / "2020qrels-pass.txt").write_text("", encoding="utf-8")
    assert parse_queries(tmp_path / "trec_dl_2019_queries.tsv")["19335"] == "what is a triangle"
    rels = parse_qrels(tmp_path / "2019qrels-pass.txt")
    assert rels["19335"] == {"42": 3, "99": 1}


def test_graded_ndcg_uses_all_gold_and_ignores_duplicates():
    from src.core.retrieval_metrics import graded_ndcg_at_k
    qrels = {"a": 3, "b": 1}
    perfect = graded_ndcg_at_k(["a", "b"], qrels, 10)
    swapped = graded_ndcg_at_k(["b", "a"], qrels, 10)
    dup = graded_ndcg_at_k(["a", "a"], qrels, 10)
    assert perfect == 1.0
    assert swapped < perfect
    assert dup < perfect


def test_collection_subset_keeps_required_pids(tmp_path):
    import random
    from src.core.trec_qrels import collect_collection_subset
    path = tmp_path / "collection.tsv"
    path.write_text("1\tgold-a\n2\tnoise\n3\tgold-b\n4\tnoise2\n", encoding="utf-8")
    out = collect_collection_subset(path, {"1", "3", "9"}, 1, random.Random(0))
    assert out["n_found_required"] == 2 and out["n_missing_required"] == 1
    assert out["passages"]["1"] == "gold-a" and "9" not in out["passages"]
    assert out["n_distractors"] == 1
    assert set(out["passages"]) - {"1", "3"} <= {"2", "4"}


def test_trec_eval_scores_passage_id_not_duplicate_text(monkeypatch):
    from scripts.eval_retrieval_datasets import eval_trec_queries, index_corpus

    class _P:
        def embed(self, texts):
            return [[1.0, 0.0] if ("target" in t or t == "q") else [0.0, 1.0] for t in texts]

    monkeypatch.setattr("scripts.eval_retrieval_datasets.get_shared_embedding_backend", lambda backend: _P())
    monkeypatch.setattr(
        "src.knowledge.memory_service.embedding_client.get_shared_embedding_backend",
        lambda backend=None: _P(),
    )
    passages = {"42": "target passage", "99": "other passage"}
    client, _ = index_corpus(list(passages.values()), "hash", "u", memory_ids=list(passages))
    out = eval_trec_queries(
        {"1": "q", "2": "unjudged"},
        ["1", "2"],
        {"1": {"42": 3, "missing": 3}},
        set(passages),
        client,
        "hash",
        "u",
        use_orchestrator=False,
    )
    assert out["n_queries_evaluated"] == 1
    assert out["hit_at_1"] == 1.0
    assert out["rank_key"] == "passage_id"
    assert out["recall_at_5"] == 0.5
    assert out["ndcg_at_10_graded"] < 1.0
    assert out["n_queries_skipped_no_positive"] == 1
    assert out["primary_metric"] == "ndcg_at_10_graded"
    assert out["n_positive_query_pid_pairs_missing_from_index"] == 1


def test_distractors_target_adds_random_negatives_despite_more_required():
    from scripts.eval_retrieval_datasets import distractors_target

    # 修复 #1：judged 正例数(7707) 远大于 rc 时，旧实现 max(0, rc - len(required))
    # 返回 0（子库只剩正例、Hit@1 被放大）。新语义下 rc 是叠加的随机干扰段数量，
    # 与正例数无关，始终取 rc。
    assert distractors_target(1200, 7707) == 1200
    assert distractors_target(5000, 0) == 5000
    assert distractors_target(0, 100) == 0


def test_trec_coverage_missing_collection_path(tmp_path):
    git = tmp_path
    (git / "TREC").mkdir()
    (git / "TREC" / "trec_dl_2019_queries.tsv").write_text("1\tfoo\n", encoding="utf-8")
    (git / "TREC" / "trec_dl_2020_queries.tsv").write_text("2\tbar\n", encoding="utf-8")
    (git / "TREC" / "2019qrels-pass.txt").write_text("1 Q0 9 2\n", encoding="utf-8")
    (git / "TREC" / "2020qrels-pass.txt").write_text("2 Q0 8 1\n", encoding="utf-8")
    cov = trec_coverage(git, tmp_path / "missing.tsv")
    assert cov["official_eval"] == "skipped"
    assert "does not exist" in cov["reason"]


def test_trec_coverage_skips_without_passage_collection(tmp_path):
    git = tmp_path
    (git / "TREC").mkdir()
    (git / "TREC" / "trec_dl_2019_queries.tsv").write_text("1\tfoo\n", encoding="utf-8")
    (git / "TREC" / "trec_dl_2020_queries.tsv").write_text("2\tbar\n", encoding="utf-8")
    (git / "TREC" / "2019qrels-pass.txt").write_text("1 Q0 9 2\n", encoding="utf-8")
    (git / "TREC" / "2020qrels-pass.txt").write_text("2 Q0 8 1\n", encoding="utf-8")
    cov = trec_coverage(git)
    assert cov["official_eval"] == "skipped"
    assert cov["n_queries"] == 2
    assert cov["n_positive_passage_ids"] == 2
    assert "8.8M" in cov["reason"]
