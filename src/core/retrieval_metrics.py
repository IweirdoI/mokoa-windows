"""Binary relevance metrics with complete gold denominators."""
import math


def graded_ndcg_at_k(ranked: list[str], qrels: dict[str, int], k: int = 10) -> float | None:
    """nDCG with gain 2^rel-1. Unranked gold still counts in the ideal DCG."""
    positives = {pid: rel for pid, rel in qrels.items() if rel > 0}
    if not positives:
        return None
    seen: set[str] = set()
    dcg = 0.0
    for i, pid in enumerate(ranked[:k]):
        if pid in seen:
            continue
        seen.add(pid)
        rel = positives.get(pid, 0)
        if rel:
            dcg += (2 ** rel - 1) / math.log2(i + 2)
    ideal = sorted(positives.values(), reverse=True)[:k]
    idcg = sum((2 ** rel - 1) / math.log2(i + 2) for i, rel in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


def binary_metrics(ranked: list[str], gold: set[str]) -> dict[str, float] | None:
    if not gold:
        return None
    seen = set()
    rels = []
    for value in ranked[:10]:
        rels.append(int(value in gold and value not in seen))
        seen.add(value)
    ideal = sum(1 / math.log2(i + 2) for i in range(min(len(gold), 10)))
    return {
        "recall_at_5": sum(rels[:5]) / len(gold),
        "mrr_at_10": next((1 / (i + 1) for i, rel in enumerate(rels) if rel), 0.0),
        "ndcg_at_10": sum(rel / math.log2(i + 2) for i, rel in enumerate(rels)) / ideal,
        "hit_at_1": float(bool(rels and rels[0])),
    }
