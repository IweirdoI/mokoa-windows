"""Parse TREC DL query/qrel files without requiring the 8.8M passage collection."""
from __future__ import annotations

import random
from collections import defaultdict
from pathlib import Path


def parse_queries(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        qid, sep, rest = line.partition("\t")
        if not sep:
            qid, sep, rest = line.partition(" ")
        if sep and rest.strip():
            out[qid.strip()] = rest.strip()
    return out


def parse_qrels(path: Path, *, min_rel: int = 1) -> dict[str, dict[str, int]]:
    """qid -> {passage_id: relevance}."""
    judged: dict[str, dict[str, int]] = defaultdict(dict)
    if not path.exists():
        return {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        qid, pid, rel = parts[0], parts[2], int(parts[3])
        judged[qid][pid] = rel
    if min_rel <= 0:
        return dict(judged)
    return {
        qid: {pid: rel for pid, rel in pids.items() if rel >= min_rel}
        for qid, pids in judged.items()
        if any(rel >= min_rel for rel in pids.values())
    }


def audit_passage_collection(path: Path, positive_ids: set[str]) -> dict:
    """Stream a ranking collection.tsv (pid TAB text), never join QA by query."""
    remaining = set(positive_ids)
    found = set()
    rows = malformed = 0
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            pid, sep, text = line.rstrip("\r\n").partition("\t")
            rows += 1
            if not sep or not pid.strip() or not text.strip():
                malformed += 1
                continue
            if pid in positive_ids:
                found.add(pid)
                remaining.discard(pid)
    return {"path": str(path), "rows": rows, "malformed_rows": malformed,
            "n_required": len(positive_ids), "n_found": len(found),
            "n_missing": len(remaining), "missing_pid_examples": sorted(remaining)[:20],
            "positive_pid_coverage_complete": bool(positive_ids) and not remaining}


def load_trec_dl(git_root: Path, *, min_rel: int = 1) -> dict:
    """Load TREC DL 2019/2020 queries and passage qrels. 2020 qids do not overlap 2019."""
    root = git_root / "TREC"
    queries = parse_queries(root / "trec_dl_2019_queries.tsv")
    queries.update(parse_queries(root / "trec_dl_2020_queries.tsv"))
    qrels_2019 = parse_qrels(root / "2019qrels-pass.txt", min_rel=min_rel)
    qrels_2020 = parse_qrels(root / "2020qrels-pass.txt", min_rel=min_rel)
    qrels = {**qrels_2019, **qrels_2020}
    return {
        "queries": queries,
        "qrels": qrels,
        "qrels_2019": qrels_2019,
        "qrels_2020": qrels_2020,
        "judged_qids": sorted(qrels),
    }


def collect_collection_subset(
    path: Path,
    required_pids: set[str],
    n_distractors: int,
    rng: random.Random,
) -> dict:
    """One-pass pid lookup plus reservoir distractors. Never drops required pids."""
    found: dict[str, str] = {}
    reservoir: list[tuple[str, str]] = []
    seen_extra = 0
    rows = malformed = 0
    want = max(int(n_distractors), 0)
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            pid, sep, text = line.rstrip("\r\n").partition("\t")
            rows += 1
            if not sep or not pid.strip() or not text.strip():
                malformed += 1
                continue
            if pid in required_pids:
                found[pid] = text
                continue
            if want <= 0:
                continue
            seen_extra += 1
            if len(reservoir) < want:
                reservoir.append((pid, text))
            else:
                j = rng.randrange(seen_extra)
                if j < want:
                    reservoir[j] = (pid, text)
    missing = sorted(required_pids - set(found))
    distractors = dict(reservoir)
    passages = {**found, **distractors}
    return {
        "passages": passages,
        "n_required": len(required_pids),
        "n_found_required": len(found),
        "n_missing_required": len(missing),
        "missing_pid_examples": missing[:20],
        "n_distractors": len(distractors),
        "rows": rows,
        "malformed_rows": malformed,
        "positive_pid_coverage_complete": bool(required_pids) and not missing,
    }


def trec_coverage(git_root: Path, collection_path: Path | None = None) -> dict:
    """Describe local TREC DL files and why official recall cannot run."""
    root = git_root / "TREC"
    queries = parse_queries(root / "trec_dl_2019_queries.tsv")
    queries.update(parse_queries(root / "trec_dl_2020_queries.tsv"))
    qrels_2019 = parse_qrels(root / "2019qrels-pass.txt")
    qrels_2020 = parse_qrels(root / "2020qrels-pass.txt")
    pids = set()
    for mapping in (qrels_2019, qrels_2020):
        for rels in mapping.values():
            pids.update(rels)
    result = {
        "path": str(root),
        "exists": root.exists(),
        "n_queries": len(queries),
        "n_judged_queries_2019": len(qrels_2019),
        "n_judged_queries_2020": len(qrels_2020),
        "n_positive_passage_ids": len(pids),
        "official_eval": "skipped",
        "reason": (
            "TREC DL qrels reference MS MARCO passage IDs from the 8.8M ranking "
            "collection; git/MS_MARCO is HuggingFace v1.1 QA (query + 10 candidates, "
            "no passage_id). Queries also do not match v1.1 query text."
        ),
    }
    if collection_path is not None:
        if not Path(collection_path).exists():
            result["reason"] = (
                f"Ranking collection path does not exist: {collection_path}"
            )
        else:
            result["passage_alignment"] = audit_passage_collection(collection_path, pids)
            result["reason"] = (
                "Ranking collection audited by passage ID. Alignment alone is not an official "
                "TREC retrieval run; full-corpus indexing and ranking are still required."
            )
    return result
