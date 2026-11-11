#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""知识检索量化评测：MS MARCO v1.1 + TREC DL 抽样检索.

TREC 官方 8.8M 全库排名仍跳过（进程内无法索引 884 万段）。段落库存在时按 pid
取出 judged query 的正例正文，再加干扰段做抽样 Recall/nDCG；正例用 passage id，
不用 QA query_id 或相似正文去猜。

    export OS_AGENT_EMBEDDING_BACKEND=kylin OS_AGENT_MEMORY_BACKEND=memory OS_AGENT_KYLIN_STRICT=1
    bash scripts/run_host_py.sh scripts/eval_retrieval_datasets.py --seed 42 --nq 50 --rc 5000
    bash scripts/run_host_py.sh scripts/eval_retrieval_datasets.py --seed 42 --trec-only --rc 5000
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

os.environ.setdefault("OS_AGENT_EMBEDDING_BACKEND", "local")
os.environ.setdefault("OS_AGENT_MEMORY_BACKEND", "memory")
try:
    from src.common.env import load_env
    load_env()
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pyarrow.parquet as pq  # noqa: E402

from src.common.models import MemoryType, RetrievalQuery  # noqa: E402
from src.core.factory import create_orchestrator  # noqa: E402
from src.core.trec_qrels import collect_collection_subset, load_trec_dl, trec_coverage  # noqa: E402
from src.core.retrieval_metrics import binary_metrics, graded_ndcg_at_k
from src.embedding.embedding_interface import get_shared_embedding_backend  # noqa: E402
from src.knowledge.memory_service.embedding_client import EmbeddingClient  # noqa: E402
from src.knowledge.memory_service.in_memory_client import InMemoryMilvusClient  # noqa: E402
from src.knowledge.memory_service.memory_schema import (  # noqa: E402
    MemoryTypeTag,
    QueryMemoryRequest,
)
from src.knowledge.memory_service.query_memory import query_memory  # noqa: E402


GIT = ROOT / "git"
DEFAULT_TREC_COLLECTION = GIT / "MS_MARCO" / "passage_ranking" / "collection.tsv"


def _pct(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    k = (len(s) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] * (1 - (k - lo)) + s[hi] * (k - lo)


def _sample_indices(rng: random.Random, total: int, n: int) -> list[int]:
    if total <= 0:
        return []
    if n >= total:
        return list(range(total))
    return sorted(rng.sample(range(total), n))


def load_msmarco_rows() -> list[dict]:
    path = GIT / "MS_MARCO" / "v1.1" / "validation-00000-of-00001.parquet"
    if not path.exists():
        return []
    return pq.read_table(str(path)).to_pylist()


def build_corpus(rows: list[dict], q_idx: list[int], corpus_target: int, rng: random.Random) -> tuple[list[str], dict[int, set[str]], dict]:
    corpus: list[str] = []
    seen: set[str] = set()
    query_positives: dict[int, set[str]] = {}
    skipped_no_pos = 0
    n_gold_dropped_as_seen = 0
    for i in q_idx:
        r = rows[i]
        passages = r.get("passages") or {}
        ptexts = passages.get("passage_text") or []
        psel = passages.get("is_selected") or []
        positives: set[str] = set()
        for t, s in zip(ptexts, psel):
            if not t:
                continue
            selected = str(s) in {"1", "1.0", "true", "True"}
            if selected:
                if t in seen:
                    n_gold_dropped_as_seen += 1
                positives.add(t)
            if t not in seen:
                seen.add(t)
                corpus.append(t)
        if positives:
            query_positives[i] = positives
        else:
            skipped_no_pos += 1
    if len(corpus) < corpus_target:
        rest = [i for i in range(len(rows)) if i not in set(q_idx)]
        add_idx = _sample_indices(rng, len(rest), min(corpus_target - len(corpus), len(rest)))
        for j in add_idx:
            for t in (rows[rest[j]].get("passages") or {}).get("passage_text") or []:
                if t and t not in seen:
                    seen.add(t)
                    corpus.append(t)
                    if len(corpus) >= corpus_target:
                        break
            if len(corpus) >= corpus_target:
                break
    stats = {
        "skipped_no_positive": skipped_no_pos,
        "gold_already_in_corpus_from_other_query": n_gold_dropped_as_seen,
        "n_eval_queries": len(query_positives),
        "mean_gold": round(
            statistics.fmean(len(v) for v in query_positives.values()), 4
        ) if query_positives else 0.0,
    }
    # Candidate sets of sampled queries are mandatory. A small distractor
    # target must never silently remove their gold passages.
    stats["corpus_target_exceeded"] = bool(corpus_target and len(corpus) > corpus_target)
    return corpus, query_positives, stats


def index_corpus(
    corpus: list[str],
    backend: str,
    user_id: str,
    memory_ids: list[str] | None = None,
) -> tuple[InMemoryMilvusClient, float]:
    provider = get_shared_embedding_backend(backend)
    ids = memory_ids or [f"c-{k}" for k in range(len(corpus))]
    if len(ids) != len(corpus):
        raise ValueError("memory_id count differs from corpus count")
    t0 = time.perf_counter()
    vecs: list = []
    for s in range(0, len(corpus), 64):
        vecs.extend(provider.embed(corpus[s:s + 64]))
    embed_s = time.perf_counter() - t0
    client = InMemoryMilvusClient()
    if len(vecs) != len(corpus):
        raise ValueError("Embedding count differs from corpus count; refusing partial index")
    for mid, text, vec in zip(ids, corpus, vecs):
        client.insert(
            memory_id=mid,
            content=text,
            embedding=vec,
            metadata={
                "user_id": user_id,
                "memory_type": MemoryTypeTag.DOCUMENT_CHUNK.value,
                "is_latest": True,
            },
        )
    return client, embed_s


def eval_queries(
    rows: list[dict],
    q_idx: list[int],
    positives: dict[int, set[str]],
    client: InMemoryMilvusClient,
    backend: str,
    user_id: str,
    *,
    use_orchestrator: bool,
    retrieval_mode: str = "auto",
) -> dict:
    orch = create_orchestrator() if use_orchestrator else None
    if orch is not None:
        orch._milvus = client  # noqa: SLF001  same index for a fair strategy contrast
    recalls, inflated, mrrs, ndcgs, hit1, lats, empty = [], [], [], [], [], [], 0
    strategies: dict[str, int] = {}
    evaluated = 0
    details = []
    for i in q_idx:
        gold = positives.get(i)
        if not gold:
            continue
        evaluated += 1
        q = str(rows[i].get("query") or "")
        if use_orchestrator:
            res = orch.recall(
                RetrievalQuery(
                    query=q,
                    user_id=user_id,
                    top_k=10,
                    memory_type=MemoryType.KNOWLEDGE,
                    retrieval_mode=retrieval_mode,
                )
            )
            ranked = [it.content for it in res.items]
            lat = res.latency_ms
            lats.append(lat)
            strategies[str(res.strategy)] = strategies.get(str(res.strategy), 0) + 1
        else:
            resp = query_memory(
                QueryMemoryRequest(user_id=user_id, query=q, top_k=10),
                milvus=client,
                embedding=EmbeddingClient(backend),
            )
            ranked = [it.get("content", "") for it in resp.items]
            lat = resp.latency_ms
            lats.append(lat)
            strategies["query_memory"] = strategies.get("query_memory", 0) + 1
        if not ranked:
            empty += 1
        rels = [1 if c in gold else 0 for c in ranked]
        metrics = binary_metrics(ranked, gold)
        recalls.append(metrics["recall_at_5"])
        inflated.append(sum(rels[:5]) / max(1, sum(rels)))
        mrrs.append(metrics["mrr_at_10"])
        ndcgs.append(metrics["ndcg_at_10"])
        hit1.append(metrics["hit_at_1"])
        details.append(
            {
                "sampled_index": i,
                "query_id": rows[i].get("query_id") or rows[i].get("queryid"),
                "query": q,
                "n_gold": len(gold),
                "recall_at_5": metrics["recall_at_5"],
                "hit_at_1": metrics["hit_at_1"],
                "latency_ms": round(lat, 3),
                "ranked_preview": [str(c)[:240] for c in ranked[:5]],
            }
        )
    return {
        "n_queries_evaluated": evaluated,
        "n_queries_skipped_no_positive": sum(not positives.get(i) for i in q_idx),
        "n_empty": empty,
        "recall_at_5": round(statistics.fmean(recalls), 4) if recalls else 0.0,
        "recall_at_5_inflated": round(statistics.fmean(inflated), 4) if inflated else 0.0,
        "mrr_at_10": round(statistics.fmean(mrrs), 4) if mrrs else 0.0,
        "ndcg_at_10": round(statistics.fmean(ndcgs), 4) if ndcgs else 0.0,
        "hit_at_1": round(statistics.fmean(hit1), 4) if hit1 else 0.0,
        "query_avg_ms": round(statistics.fmean(lats), 3) if lats else None,
        "query_p95_ms": round(_pct(lats, 95), 3) if lats else None,
        "latency_passed": bool(lats) and _pct(lats, 95) <= 500,
        "strategies": strategies,
        "details": details,
    }


def eval_trec_queries(
    queries: dict[str, str],
    qids: list[str],
    qrels: dict[str, dict[str, int]],
    indexed_pids: set[str],
    client: InMemoryMilvusClient,
    backend: str,
    user_id: str,
    *,
    use_orchestrator: bool,
    retrieval_mode: str = "auto",
) -> dict:
    orch = create_orchestrator() if use_orchestrator else None
    if orch is not None:
        orch._milvus = client  # noqa: SLF001
    recalls, inflated, mrrs, ndcgs, graded, hit1, lats, empty = [], [], [], [], [], [], [], 0
    strategies: dict[str, int] = {}
    skipped_no_gold = skipped_no_text = 0
    evaluated = 0
    gold_kept = []
    missing_positive_pairs = 0
    details = []
    for qid in qids:
        # Missing indexed positives are misses, not a smaller ground truth.
        rels_map = {pid: rel for pid, rel in qrels.get(qid, {}).items() if rel >= 1}
        if not rels_map:
            skipped_no_gold += 1
            continue
        q = (queries.get(qid) or "").strip()
        if not q:
            skipped_no_text += 1
            continue
        evaluated += 1
        gold = set(rels_map)
        missing_positive_pairs += len(gold - indexed_pids)
        gold_kept.append(len(gold))
        if use_orchestrator:
            res = orch.recall(
                RetrievalQuery(
                    query=q,
                    user_id=user_id,
                    top_k=10,
                    memory_type=MemoryType.KNOWLEDGE,
                    retrieval_mode=retrieval_mode,
                )
            )
            ranked = [it.id for it in res.items]
            lat = res.latency_ms
            lats.append(lat)
            strategies[str(res.strategy)] = strategies.get(str(res.strategy), 0) + 1
        else:
            resp = query_memory(
                QueryMemoryRequest(user_id=user_id, query=q, top_k=10),
                milvus=client,
                embedding=EmbeddingClient(backend),
            )
            ranked = [str(it.get("memory_id") or "") for it in resp.items]
            lat = resp.latency_ms
            lats.append(lat)
            strategies["query_memory"] = strategies.get("query_memory", 0) + 1
        if not ranked:
            empty += 1
        hits = [1 if pid in gold else 0 for pid in ranked]
        metrics = binary_metrics(ranked, gold)
        if metrics is None:
            skipped_no_gold += 1
            evaluated -= 1
            continue
        recalls.append(metrics["recall_at_5"])
        inflated.append(sum(hits[:5]) / max(1, sum(hits)))
        mrrs.append(metrics["mrr_at_10"])
        ndcgs.append(metrics["ndcg_at_10"])
        graded.append(graded_ndcg_at_k(ranked, rels_map, 10) or 0.0)
        hit1.append(metrics["hit_at_1"])
        details.append(
            {
                "qid": qid,
                "query": q,
                "n_gold": len(gold),
                "recall_at_5": metrics["recall_at_5"],
                "hit_at_1": metrics["hit_at_1"],
                "ndcg_at_10_graded": graded[-1],
                "latency_ms": round(lat, 3),
                "ranked_pids": ranked[:10],
            }
        )
    return {
        "n_queries_evaluated": evaluated,
        "n_queries_skipped_no_positive": skipped_no_gold,
        "n_queries_skipped_no_text": skipped_no_text,
        "n_empty": empty,
        "mean_gold": round(statistics.fmean(gold_kept), 4) if gold_kept else 0.0,
        "recall_at_5": round(statistics.fmean(recalls), 4) if recalls else 0.0,
        "recall_at_5_inflated": round(statistics.fmean(inflated), 4) if inflated else 0.0,
        "mrr_at_10": round(statistics.fmean(mrrs), 4) if mrrs else 0.0,
        "ndcg_at_10": round(statistics.fmean(ndcgs), 4) if ndcgs else 0.0,
        "ndcg_at_10_graded": round(statistics.fmean(graded), 4) if graded else 0.0,
        "hit_at_1": round(statistics.fmean(hit1), 4) if hit1 else 0.0,
        "query_avg_ms": round(statistics.fmean(lats), 3) if lats else None,
        "query_p95_ms": round(_pct(lats, 95), 3) if lats else None,
        "latency_passed": bool(lats) and _pct(lats, 95) <= 500,
        "strategies": strategies,
        "rank_key": "passage_id",
        "primary_metric": "ndcg_at_10_graded",
        "relevance_scope": "all positive qrels, including positives absent from the index",
        "n_positive_query_pid_pairs_missing_from_index": missing_positive_pairs,
        "evaluation_scope": "sampled corpus; not official full-collection TREC ranking",
        "details": details,
    }


def query_id_overlap(rows: list[dict], git_root: Path) -> dict:
    from src.core.trec_qrels import parse_queries

    trec_q = parse_queries(git_root / "TREC" / "trec_dl_2019_queries.tsv")
    trec_q.update(parse_queries(git_root / "TREC" / "trec_dl_2020_queries.tsv"))
    marco_text = {(r.get("query") or "").strip().lower() for r in rows}
    marco_ids = {str(r.get("query_id")) for r in rows}
    return {
        "trec_queries": len(trec_q),
        "msmarco_val_queries": len(marco_text),
        "exact_text_overlap": sum(1 for q in trec_q.values() if q.lower() in marco_text),
        "query_id_overlap": sum(1 for qid in trec_q if qid in marco_ids),
    }


def distractors_target(rc: int, n_required: int) -> int:
    """计算从 8.8M 全库抽样的随机干扰段数量（修复 #1）。

    ``rc`` 表示叠加在 judged 金标准正例之上的随机干扰段数量，不是总语料上限。
    旧实现 ``max(0, rc - len(required))`` 在 ``rc`` 小于正例数时恒为 0，使子库只剩
    正例（每个分段都是某问的正例）、Hit@1 被放大。这里直接取 ``rc``，保证有真实负例。
    """
    return max(0, int(rc))


def run_trec_sampled(
    collection: Path,
    rng: random.Random,
    backend: str,
    *,
    rc: int,
    trec_nq: int,
) -> dict:
    topics = load_trec_dl(GIT)
    judged = topics["judged_qids"]
    if not judged:
        return {"sampled_eval": "skipped", "reason": "no judged TREC DL passage qrels on disk"}
    if trec_nq and 0 < trec_nq < len(judged):
        qids = sorted(rng.sample(judged, trec_nq))
        sampled = True
    else:
        qids = judged
        sampled = False
    required: set[str] = set()
    for qid in qids:
        required.update(topics["qrels"][qid])
    # 修复 #1：rc 现表示「叠加在金标准正例之上的随机干扰段数量」，而非总语料上限。
    # 旧逻辑 n_distractors = max(0, rc - len(required)) 在 rc 小于正例数时会恒为 0，
    # 导致子库只剩正例、Hit@1 被放大（每个分段都是某问的正例，没有真实负例）。
    n_distractors = distractors_target(rc, len(required))
    print(f"      judged={len(judged)} eval_q={len(qids)} gold_pids={len(required)} "
          f"distractor_target={n_distractors}", flush=True)
    subset = collect_collection_subset(collection, required, n_distractors, rng)
    print(f"      collection found={subset['n_found_required']}/{subset['n_required']} "
          f"missing={subset['n_missing_required']} distractors={subset['n_distractors']}", flush=True)
    if subset["n_found_required"] == 0:
        return {
            "sampled_eval": "skipped",
            "reason": "collection.tsv had none of the judged passage ids",
            "subset": {k: v for k, v in subset.items() if k != "passages"},
            "eval_qids": qids,
        }
    pids = list(subset["passages"])
    texts = [subset["passages"][pid] for pid in pids]
    uid = "ret_trec"
    print(f"      encoding {len(texts)} passages ...", flush=True)
    client, embed_s = index_corpus(texts, backend, uid, memory_ids=pids)
    print(f"      embed_s={embed_s:.2f}", flush=True)
    indexed = set(pids)
    qm = eval_trec_queries(
        topics["queries"], qids, topics["qrels"], indexed, client, backend, uid,
        use_orchestrator=False,
    )
    orch = eval_trec_queries(
        topics["queries"], qids, topics["qrels"], indexed, client, backend, uid,
        use_orchestrator=True,
    )
    semantic = eval_trec_queries(
        topics["queries"], qids, topics["qrels"], indexed, client, backend, uid,
        use_orchestrator=True, retrieval_mode="semantic",
    )
    return {
        "sampled_eval": "ran",
        "official_eval": "skipped",
        "official_reason": (
            "Full-corpus TREC DL ranking needs all 8,841,823 passages indexed; "
            "in-memory cosine cannot hold that collection."
        ),
        "scope": (
            "in-memory cosine over judged gold pids"
            + (f" plus {subset['n_distractors']} random distractors sampled from 8.8M collection"
               if subset["n_distractors"] else " (no random negatives — Hit@1 is inflated; for reference only)")
            + "; not official TREC 8.8M nDCG"
        ),
        "eval_qids": qids,
        "n_judged_available": len(judged),
        "sampled_from_judged": sampled,
        "n_gold_pids_selected": len(required),
        "distractor_requested": rc,
        "distractor_collected": subset["n_distractors"],
        "corpus_size": len(texts),
        "corpus_embed_seconds": round(embed_s, 2),
        "corpus_target_exceeded": bool(rc) and subset["n_distractors"] < rc,
        "subset": {
            **{k: v for k, v in subset.items() if k != "passages"},
            "distractor_pids": sorted(
                pid for pid in subset["passages"] if pid not in required
            ),
        },
        "query_memory": qm,
        "orchestrator_recall": orch,
        "orchestrator_semantic": semantic,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--nq", type=int, default=50)
    ap.add_argument(
        "--rc", type=int, default=5000,
        help="从 8.8M 全库 reservoir 抽样的随机干扰段数量，叠加在 judged 金标准正例之上"
             "（不是总语料上限）。设 0 则回退到仅正例子库（Hit@1 会被放大，仅供对照）。",
    )
    ap.add_argument("--out", type=str, default="benchmarks/results")
    ap.add_argument("--trec-nq", type=int, default=0, help="0 = all judged TREC DL queries")
    ap.add_argument("--trec-only", action="store_true", help="Skip MS MARCO v1.1 sampled eval")
    ap.add_argument(
        "--trec-collection",
        type=Path,
        default=DEFAULT_TREC_COLLECTION if DEFAULT_TREC_COLLECTION.exists() else None,
        help="MS MARCO passage ranking collection.tsv (pid TAB text)",
    )
    args = ap.parse_args()
    backend = os.environ.get("OS_AGENT_EMBEDDING_BACKEND", "local")
    get_shared_embedding_backend(backend)

    print("[1/4] TREC 覆盖诊断（无 8.8M 则跳过官方全库排名）...", flush=True)
    trec = trec_coverage(GIT, args.trec_collection)
    print(f"      queries={trec['n_queries']} judged2019={trec['n_judged_queries_2019']} "
          f"judged2020={trec['n_judged_queries_2020']} pids={trec['n_positive_passage_ids']} "
          f"official={trec['official_eval']}", flush=True)
    align = trec.get("passage_alignment")
    if align:
        print(f"      collection rows={align['rows']} found={align['n_found']}/"
              f"{align['n_required']} missing={align['n_missing']}", flush=True)

    trec_run: dict
    if args.trec_collection is None or not Path(args.trec_collection).exists():
        trec_run = {
            "sampled_eval": "skipped",
            "reason": "git/MS_MARCO/passage_ranking/collection.tsv missing; cannot score TREC by pid",
        }
        print("      sampled TREC skipped: no ranking collection", flush=True)
    else:
        print("[1b/4] TREC 抽样检索（按 pid，非 8.8M 全库）...", flush=True)
        try:
            trec_run = run_trec_sampled(
                Path(args.trec_collection), random.Random(args.seed), backend,
                rc=args.rc, trec_nq=args.trec_nq,
            )
        except Exception as exc:
            import traceback
            traceback.print_exc()
            trec_run = {
                "sampled_eval": "skipped",
                "reason": f"{type(exc).__name__}: {exc}",
            }
            print(f"      sampled TREC skipped: {trec_run['reason']}", flush=True)

    overlap = {"trec_queries": trec["n_queries"], "msmarco_val_queries": 0,
               "exact_text_overlap": 0, "query_id_overlap": 0}
    msmarco: dict | None = None
    qm = orch = semantic = gold_stats = None
    q_idx: list[int] = []
    if not args.trec_only:
        print("[2/4] 加载 MS MARCO v1.1 validation ...", flush=True)
        rows = load_msmarco_rows()
        if not rows:
            raise RuntimeError("MS MARCO validation parquet missing under git/MS_MARCO/v1.1")
        overlap = query_id_overlap(rows, GIT)
        print(f"      val={len(rows)} trec_text_overlap={overlap['exact_text_overlap']} "
              f"qid_overlap={overlap['query_id_overlap']}", flush=True)

        q_idx = _sample_indices(random.Random(args.seed), len(rows), args.nq)
        corpus, positives, gold_stats = build_corpus(rows, q_idx, args.rc, random.Random(args.seed))
        print(f"      corpus={len(corpus)} eval_q={gold_stats['n_eval_queries']} "
              f"skip_no_pos={gold_stats['skipped_no_positive']}", flush=True)
        if gold_stats.get("corpus_target_exceeded"):
            print("      [警告] corpus 规模超过 --rc 目标，多余 distractor 未加入；"
                  "抽样 query 的 gold 不受影响", flush=True)

        print("[3/4] 编码入库 ...", flush=True)
        uid = "ret_marco"
        client, embed_s = index_corpus(corpus, backend, uid)
        print(f"      embed_s={embed_s:.2f}", flush=True)

        print("[4/4] query_memory + Orchestrator.recall ...", flush=True)
        qm = eval_queries(rows, q_idx, positives, client, backend, uid, use_orchestrator=False)
        orch = eval_queries(rows, q_idx, positives, client, backend, uid, use_orchestrator=True)
        semantic = eval_queries(rows, q_idx, positives, client, backend, uid,
                                use_orchestrator=True, retrieval_mode="semantic")
        msmarco = {
            "sampled_indices": q_idx,
            "corpus_size": len(corpus),
            "corpus_embed_seconds": round(embed_s, 2),
            "gold": gold_stats,
            "query_memory": qm,
            "orchestrator_recall": orch,
            "orchestrator_semantic": semantic,
            "scope": "in-memory cosine over sampled candidates+distractors; not MS MARCO 8.8M or TREC official",
        }

    results = {
        "meta": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "seed": args.seed,
            "nq": args.nq,
            "trec_nq": args.trec_nq,
            "rc": args.rc,
            "trec_only": args.trec_only,
            "embedding_backend": backend,
            "memory_backend": os.environ.get("OS_AGENT_MEMORY_BACKEND", "memory"),
            "commit": subprocess.check_output(
                ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], text=True
            ).strip(),
            "eval_code_commit": "17af2b3f509ecea1ed10f1b02134277e530775ed",
            "rng": {
                "implementation": "Python random.Random (CPython Mersenne Twister)",
                "seed": args.seed,
                "msmarco_sampling_fn": "scripts.eval_retrieval_datasets._sample_indices",
                "msmarco_algorithm": "sorted(rng.sample(range(total), nq)); independent Random(seed) for distractors in build_corpus",
                "trec_sampling_fn": (
                    "all judged qids when trec_nq=0; distractors via "
                    "src.core.trec_qrels.collect_collection_subset Vitter reservoir"
                ),
            },
        },
        "trec": {**trec, "overlap_with_msmarco_val": overlap, **trec_run},
        "msmarco": msmarco if msmarco is not None else {"sampled_eval": "skipped", "reason": "--trec-only"},
    }
    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = out_dir / f"retrieval_{ts}.json"
    json_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "report": str(json_path),
        "trec": {
            "official_eval": trec["official_eval"],
            "sampled_eval": trec_run.get("sampled_eval"),
            "reason": trec_run.get("reason") or trec_run.get("official_reason") or trec.get("reason"),
            "n_queries": trec["n_queries"],
            "n_judged_2019": trec["n_judged_queries_2019"],
            "query_memory": trec_run.get("query_memory"),
            "orchestrator_recall": trec_run.get("orchestrator_recall"),
            "orchestrator_semantic": trec_run.get("orchestrator_semantic"),
            "overlap": overlap,
        },
        "msmarco_query_memory": qm,
        "msmarco_orchestrator_recall": orch,
        "msmarco_orchestrator_semantic": semantic,
        "gold": gold_stats,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n报告: {json_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
