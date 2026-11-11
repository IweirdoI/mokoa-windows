#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""偏好提取准确率抽测：user-preference-564k + 封闭键金标准。

开放域金标准是 condition-action 自由文本（HF blackhao0426/user-preference-564k），
系统抽取的是 OS Agent 封闭键 key=value。本脚本同时报告：

1. 开放域 100 条：检测（空/非空）+ action 词面覆盖的样本正确率 / P / R / F1
2. 封闭键 35 条：`preference_extraction.jsonl` 的精确 key=value 匹配

    export OS_AGENT_EMBEDDING_BACKEND=kylin OS_AGENT_MEMORY_BACKEND=memory OS_AGENT_KYLIN_STRICT=1
    bash scripts/run_host_py.sh scripts/eval_preference_hf_dataset.py --seed 42 --n 100 --mode rule
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
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

from scripts.pref_eval_dataset import evaluate as evaluate_closed_set  # noqa: E402
from src.common.models import DataSource, RawRecord  # noqa: E402
from src.preference.store import PreferenceMemoryStore  # noqa: E402
from src.preference.llm_client import SiliconFlowClient  # noqa: E402


def build_llm_client(endpoint: str = "remote") -> SiliconFlowClient:
    """按 endpoint 构造抽取/判卷用的 LLM 客户端。

    - remote: 显式使用 .env 的 SILICONFLOW_*（远程强模型），用于竞赛评测。
      不能依赖无参 SiliconFlowClient()：其默认优先级为「本地 LM Studio 优先」
      （见 src/preference/llm_client.py 的 fusion 静默降级修复），而评测机通常
      没有本地 LM Studio，会在 127.0.0.1:1234 上整批超时、退化成 rule。
    - local: 无参构造，走默认优先级（本地 LM Studio 优先），供本机调试。
    """
    if endpoint == "local":
        return SiliconFlowClient()
    return SiliconFlowClient(
        api_key=os.getenv("SILICONFLOW_API_KEY"),
        base_url=os.getenv("SILICONFLOW_BASE_URL"),
        model=os.getenv("SILICONFLOW_LLM_MODEL"),
    )


GIT = ROOT / "git"
HF_JSONL_URLS = (
    "https://huggingface.co/datasets/blackhao0426/user-preference-564k"
    "/resolve/main/preference_extractor_564k.jsonl",
    "https://hf-mirror.com/datasets/blackhao0426/user-preference-564k"
    "/resolve/main/preference_extractor_564k.jsonl",
)
CACHE_DIR = GIT / "user-preference-564k"
DEFAULT_CACHE = CACHE_DIR / "sampled_seed42_n100.jsonl"

_STOP = frozenset(
    {
        "the",
        "a",
        "an",
        "to",
        "of",
        "and",
        "or",
        "for",
        "with",
        "in",
        "on",
        "at",
        "by",
        "from",
        "that",
        "this",
        "it",
        "is",
        "be",
        "are",
        "was",
        "as",
        "use",
        "using",
        "when",
        "your",
        "you",
        "please",
        "just",
        "only",
        "not",
        "no",
        "if",
        "then",
        "than",
        "into",
        "about",
        "response",
        "responses",
        "answer",
        "answers",
        "general",
        "assistant",
        "user",
        "should",
        "must",
        "will",
        "can",
        "need",
        "needed",
        "make",
        "include",
        "without",
        "each",
        "them",
        "they",
        "their",
    }
)
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]{2,}|[\u4e00-\u9fff]{1,}")


def _tokens(text: str) -> set[str]:
    out: set[str] = set()
    for raw in _TOKEN_RE.findall(text.lower()):
        tok = raw.strip("-_").lower()
        if not tok or tok in _STOP:
            continue
        out.add(tok)
    return out


def parse_gold_preferences(output) -> list[dict]:
    if output is None:
        return []
    data = output
    if isinstance(output, str):
        text = output.strip()
        if not text:
            return []
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []
    if not isinstance(data, dict):
        return []
    prefs = data.get("preferences") or []
    if not isinstance(prefs, list):
        return []
    cleaned: list[dict] = []
    for item in prefs:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or "").strip()
        condition = str(item.get("condition") or "").strip()
        if not action and not condition:
            continue
        cleaned.append(
            {
                "condition": condition,
                "action": action,
                "confidence": item.get("confidence"),
            }
        )
    return cleaned


def _action_hit(gold_action: str, extracted_blob: str) -> bool:
    gold = _tokens(gold_action)
    if len(gold) < 2:
        # 太短的 action 只认子串，避免单停用词误命中
        needle = gold_action.strip().lower()
        return len(needle) >= 4 and needle in extracted_blob.lower()
    got = _tokens(extracted_blob)
    distinctive = {t for t in gold if len(t) >= 4}
    if distinctive and distinctive <= got:
        return True
    inter = gold & got
    return len(inter) >= 2 and (len(inter) / len(gold)) >= 0.5


def _entry_blob(entry) -> str:
    meta = entry.metadata or {}
    parts = [
        str(meta.get("preference_key") or ""),
        str(meta.get("preference_value") or ""),
        # 纳入自然语言描述：系统抽到的偏好以 key=value + 中文 description 表达，
        # gold 是英文自由文本 action。仅比 key=value 词面会低估系统真实覆盖，
        # 加入 description 让匹配更公平地反映「系统是否理解到该偏好」。
        str(meta.get("description") or ""),
        str(getattr(entry, "content", "") or ""),
        str(meta.get("category") or ""),
    ]
    return " ".join(parts)


def _semantic_cover(client, gold_action: str, entries) -> bool:
    """用 LLM-judge 判断系统抽到的封闭键是否语义覆盖 gold action。

    词面 token 重叠在中英鸿沟下恒为 0（开放域 20% 的结构性根因）。本函数提供
    「语义对齐」公平口径：系统确实抽到了偏好，只是以 key=value 表达，判断是否
    等价于用户自由文本偏好。按「核心诉求」判定：忽略用词/详略/中英差异，复合金标
    覆盖主要诉求即判 YES；但仍要求语义相关，不因字面不同就误判为未覆盖。
    """
    if not gold_action or not entries:
        return False
    lines = []
    for e in entries:
        # 用完整的 _entry_blob（key=value + description + content）而非仅 metadata，
        # 否则开放域抽取头写入 entry.content 的偏好描述对判卷不可见，会误判为未覆盖。
        blob = _entry_blob(e).strip()
        if blob:
            lines.append(f"- {blob}")
    if not lines:
        return False
    prompt = (
        "你是偏好覆盖判定器。给定用户表达的一条偏好(gold)和系统抽出的偏好列表(entries)，\n"
        "判断系统抽出的偏好是否覆盖了该用户偏好的核心诉求。\n\n"
        f"用户偏好(gold): {gold_action}\n\n系统抽出的偏好:\n"
        + "\n".join(lines)
        + "\n\n在语义层面判定，不要做字面比对。忽略用词、详略、以及中英文差异。"
        "例如 gold 说『回应要有反思性、具美感的语调』而系统抽到『respond in a reflective, "
        "aesthetically thoughtful tone / beautiful and reflective』应判 YES。\n\n"
        "判定规则：\n"
        "- YES：系统抽出的某条偏好表达了 gold 的核心诉求（同义改写、更具体均可）。\n"
        "- YES：gold 是复合要求（一句话含多个子要求）时，只要系统抽出的偏好覆盖了其主要诉求\n"
        "  且与其余部分不矛盾，就判 YES。\n"
        "- NO：系统抽出的偏好与 gold 无关，或只沾边、明显不是同一诉求。\n\n"
        "只回答 YES 或 NO，并给一行简短理由。"
    )
    try:
        out = client.chat_completion(
            [{"role": "user", "content": prompt}], temperature=0.0, max_tokens=200
        )
    except Exception:
        # 调用失败（无 key / 401 / 网络错误）返回 None 哨兵，区别于「判定为未覆盖」，
        # 调用方据此单独计入 semantic_errors，避免把 LLM 失败伪装成 0% 覆盖。
        return None
    first = out.strip().split("\n", 1)[0].strip().upper()
    return first.startswith("YES")


def _git_short_head(root: Path) -> str:
    """读取当前 git commit 短哈希；仓库缺失/无 git 时兜底为 unknown，避免评测报告丢失。"""
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"], text=True
        ).strip()
    except Exception:
        return "unknown"


def _got_pairs(entries) -> list[dict]:
    rows = []
    for entry in entries:
        meta = entry.metadata or {}
        key = meta.get("preference_key")
        val = meta.get("preference_value")
        rows.append(
            {
                "preference_key": key,
                "preference_value": val,
                "pair": f"{key}={val}" if key is not None else None,
                "content": str(getattr(entry, "content", "") or ""),
            }
        )
    return rows


def match_sample(gold_prefs: list[dict], entries) -> dict:
    blobs = [_entry_blob(e) for e in entries]
    combined = " \n ".join(blobs)
    matched = 0
    details = []
    for pref in gold_prefs:
        action = pref.get("action") or ""
        hit = bool(blobs) and _action_hit(action, combined)
        matched += int(hit)
        details.append({"action": action[:180], "hit": hit})
    n_gold = len(gold_prefs)
    n_got = len(entries)
    if n_gold == 0:
        sample_ok = n_got == 0
        detection_ok = n_got == 0
        recall = 1.0 if n_got == 0 else 0.0
        precision = 1.0 if n_got == 0 else 0.0
    else:
        recall = matched / n_gold
        # 开放域没有 1:1 抽取条，精确率按「有覆盖则抽到的条算已用」：覆盖到任意金标则不把多抽当全错
        precision = 1.0 if matched else 0.0
        detection_ok = n_got > 0
        sample_ok = recall >= 0.5
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall)
        else 0.0
    )
    return {
        "n_gold": n_gold,
        "n_got": n_got,
        "gold_matched": matched,
        "recall": round(recall, 4),
        "precision": round(precision, 4),
        "f1": round(f1, 4),
        "detection_ok": detection_ok,
        "sample_ok": sample_ok,
        "gold_hits": details,
    }


def _open_stream(path_or_url: str):
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        req = urllib.request.Request(
            path_or_url,
            headers={"User-Agent": "MOKOA-eval/1.0"},
        )
        return urllib.request.urlopen(req, timeout=120)
    return Path(path_or_url).open("rb")


def sample_hf_rows(
    source: str,
    *,
    n_pos: int,
    n_neg: int,
    seed: int,
) -> tuple[list[dict], dict]:
    rng = random.Random(seed)
    pos: list[tuple[int, dict]] = []
    neg: list[tuple[int, dict]] = []
    n_seen_pos = n_seen_neg = 0
    n_lines = 0
    n_bad = 0
    with _open_stream(source) as fh:
        for raw in fh:
            n_lines += 1
            if isinstance(raw, bytes):
                line = raw.decode("utf-8", errors="replace")
            else:
                line = raw
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                n_bad += 1
                continue
            prefs = parse_gold_preferences(row.get("output"))
            item = {
                "index": n_lines - 1,
                "input": row.get("input") or "",
                "output": row.get("output"),
                "source": row.get("source"),
                "gold_preferences": prefs,
            }
            if prefs:
                n_seen_pos += 1
                if len(pos) < n_pos:
                    pos.append((item["index"], item))
                else:
                    j = rng.randint(0, n_seen_pos - 1)
                    if j < n_pos:
                        pos[j] = (item["index"], item)
            else:
                n_seen_neg += 1
                if len(neg) < n_neg:
                    neg.append((item["index"], item))
                else:
                    j = rng.randint(0, n_seen_neg - 1)
                    if j < n_neg:
                        neg[j] = (item["index"], item)
            if n_lines % 50000 == 0:
                print(
                    f"      scanned={n_lines} pos_pool={n_seen_pos} neg_pool={n_seen_neg}",
                    flush=True,
                )

    pos.sort(key=lambda x: x[0])
    neg.sort(key=lambda x: x[0])
    rows = [x[1] for x in pos + neg]
    stats = {
        "source": source,
        "n_lines": n_lines,
        "n_bad_json": n_bad,
        "n_nonempty_in_corpus": n_seen_pos,
        "n_empty_in_corpus": n_seen_neg,
        "n_pos_sampled": len(pos),
        "n_neg_sampled": len(neg),
        "sampled_indices": [r["index"] for r in rows],
        "seed": seed,
    }
    return rows, stats


def load_cached_samples(path: Path) -> tuple[list[dict], dict] | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and "rows" in payload:
        rows = payload["rows"]
        stats = payload.get("sample_stats") or {}
        return rows, stats
    if isinstance(payload, list):
        return payload, {
            "source": str(path),
            "cached_list": True,
            "n_total_sampled": len(payload),
        }
    return None


def save_cached_samples(path: Path, rows: list[dict], stats: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"sample_stats": stats, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _is_retryable(exc) -> bool:
    """并发跑远程 LLM 时识别可重试的瞬时错误（限流 / 连接 / 超时 / 5xx）。"""
    try:
        from openai import (
            APIConnectionError,
            APIError,
            APITimeoutError,
            RateLimitError,
        )
    except Exception:
        return True
    return isinstance(exc, (RateLimitError, APIConnectionError, APITimeoutError, APIError))


def _with_retry(fn, what: str, max_retries: int = 3):
    delay = 1.0
    last: Exception | None = None
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 仅对可重试错误退避
            last = exc
            if not _is_retryable(exc) or attempt >= max_retries - 1:
                raise
            print(
                f"      [retry {attempt + 1}/{max_retries}] {what}: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            time.sleep(delay)
            delay *= 2
    assert last is not None
    raise last


def _process_one_sample(mode: str, row: dict, semantic: bool, llm_client) -> tuple[dict, dict]:
    """单样本：独立临时 store 抽取 + 可选语义判卷（线程安全，共享 HTTP client）。

    每个样本各自建一个 PreferenceMemoryStore，避免并发写同一 VersionIndex 文件；
    SiliconFlowClient(OpenAI/httpx) 的连接池对并发请求安全，可跨线程共享。
    """
    use_llm = mode in ("llm", "fusion")
    allow_rules = mode in ("rule", "fusion")
    text = str(row.get("input") or "")
    gold = row.get("gold_preferences") or parse_gold_preferences(row.get("output"))
    user_id = f"hf_pref_{row.get('index')}"
    record = RawRecord(
        id=user_id,
        source=DataSource.CONVERSATION,
        user_id=user_id,
        scene_id="general",
        payload={"content": text},
    )

    def _extract() -> list:
        with tempfile.TemporaryDirectory(prefix="pref_hf_") as tmp:
            store = PreferenceMemoryStore(
                storage_dir=tmp,
                use_llm=use_llm,
                allow_rules=allow_rules,
                llm_client=llm_client,
                # 开放域口径：开启自由偏好抽取头，让系统真正抽取 condition→action 偏好，
                # 而非只匹配 OS Agent 封闭键。
                open_domain=True,
            )
            return store.extract([record])

    entries = _with_retry(_extract, f"extract idx={row.get('index')}")
    matched = match_sample(gold, entries)
    has_gold = matched["n_gold"] > 0
    has_got = matched["n_got"] > 0

    sem_gold: list[dict] = []
    sem_gold_total = sem_gold_matched = sem_err_total = 0
    n_sem_ok = 0
    if semantic and llm_client is not None:
        if gold:
            s_valid = s_hits = s_err = 0
            for p in gold:
                cov = _with_retry(
                    lambda p=p: _semantic_cover(
                        llm_client, p.get("action") or "", entries
                    ),
                    f"sem idx={row.get('index')}",
                )
                if cov is None:
                    s_err += 1
                else:
                    s_valid += 1
                    s_hits += int(cov)
                sem_gold.append(
                    {
                        "action": p.get("action") or "",
                        "condition": p.get("condition") or "",
                        "covered": bool(cov),
                    }
                )
            sem_gold_total = s_valid
            sem_gold_matched = s_hits
            n_sem_ok = int(s_valid > 0 and (s_hits / s_valid) >= 0.5)
            sem_err_total = s_err
        else:
            n_sem_ok = int(not has_got)

    tp = fp = fn = tn = 0
    if has_gold and has_got:
        tp = 1
    elif has_gold and not has_got:
        fn = 1
    elif (not has_gold) and has_got:
        fp = 1
    else:
        tn = 1

    counters = {
        "n_ok": int(matched["sample_ok"]),
        "n_detect": int(matched["detection_ok"]),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "gold_total": matched["n_gold"],
        "gold_matched": matched["gold_matched"],
        "sem_gold_total": sem_gold_total,
        "sem_gold_matched": sem_gold_matched,
        "sem_err_total": sem_err_total,
        "n_sem_ok": n_sem_ok,
    }
    detail = {
        "index": row.get("index"),
        "source": row.get("source"),
        "n_gold": matched["n_gold"],
        "n_got": matched["n_got"],
        "sample_ok": matched["sample_ok"],
        "detection_ok": matched["detection_ok"],
        "recall": matched["recall"],
        "got": _got_pairs(entries),
        "gold_preferences": gold,
        "gold_actions": [p.get("action") for p in gold],
        "gold_output_raw": row.get("output"),
        "sem_gold": sem_gold,
        "input": text,
        "input_preview": text[:180].replace("\n", " "),
    }
    return detail, counters


def extract_batch(
    mode: str,
    rows: list[dict],
    *,
    semantic: bool = False,
    client=None,
    llm_client=None,
    workers: int = 8,
) -> list[dict]:
    details = []
    n_ok = n_detect = 0
    tp = fp = fn = tn = 0
    gold_matched = gold_total = 0
    sem_gold_matched = sem_gold_total = 0
    sem_err_total = 0
    n_sem_ok = 0
    judge = client or llm_client
    workers = max(1, int(workers))
    with ThreadPoolExecutor(max_workers=workers) as _ex:
        _futs = [
            _ex.submit(_process_one_sample, mode, row, semantic, judge)
            for row in rows
        ]
        _done = 0
        for _fut in as_completed(_futs):
            _detail, _cc = _fut.result()
            details.append(_detail)
            n_ok += _cc["n_ok"]
            n_detect += _cc["n_detect"]
            tp += _cc["tp"]
            fp += _cc["fp"]
            fn += _cc["fn"]
            tn += _cc["tn"]
            gold_total += _cc["gold_total"]
            gold_matched += _cc["gold_matched"]
            sem_gold_total += _cc["sem_gold_total"]
            sem_gold_matched += _cc["sem_gold_matched"]
            sem_err_total += _cc["sem_err_total"]
            n_sem_ok += _cc["n_sem_ok"]
            _done += 1
            if _done % 20 == 0:
                print(f"      extracted {_done}/{len(rows)} mode={mode}", flush=True)

    details.sort(key=lambda d: (d["index"] if d["index"] is not None else -1))
    n = len(rows)
    detection_acc = (tp + tn) / n if n else 0.0
    sample_acc = n_ok / n if n else 0.0
    det_p = tp / (tp + fp) if (tp + fp) else 0.0
    det_r = tp / (tp + fn) if (tp + fn) else 0.0
    det_f1 = (
        2 * det_p * det_r / (det_p + det_r) if (det_p + det_r) else 0.0
    )
    action_recall = gold_matched / gold_total if gold_total else 0.0
    return {
        "mode": mode,
        "samples": n,
        "sample_accuracy": round(sample_acc, 4),
        "sample_correct": n_ok,
        "detection_accuracy": round(detection_acc, 4),
        "detection": {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "tn": tn,
            "precision": round(det_p, 4),
            "recall": round(det_r, 4),
            "f1": round(det_f1, 4),
        },
        "gold_action_recall": round(action_recall, 4),
        "gold_actions_total": gold_total,
        "gold_actions_matched": gold_matched,
        "semantic_recall": (
            round(sem_gold_matched / sem_gold_total, 4) if sem_gold_total else None
        ),
        "semantic_sample_accuracy": (
            round(n_sem_ok / n, 4) if (semantic and n) else None
        ),
        "semantic_errors": sem_err_total,
        # 开启 --semantic 时以语义样本准确率为过线判据（开放域唯一公平口径）；
        # 否则退回 token 口径。
        "pass_85": (
            (n_sem_ok / n) >= 0.85 if (semantic and n) else sample_acc >= 0.85
        ),
        "pass_85_token": sample_acc >= 0.85,
        "details": details,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--n-pos", type=int, default=80)
    ap.add_argument("--n-neg", type=int, default=20)
    ap.add_argument(
        "--mode",
        choices=["rule", "llm", "fusion", "both"],
        default="rule",
        help="rule=离线规则（默认可复现）；fusion 会调 LLM；both=先 rule 再 fusion",
    )
    ap.add_argument(
        "--hf-source",
        default="",
        help="本地 jsonl 或 HF url；默认先用缓存，没有再拉 HF",
    )
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--out", type=str, default="docs/evidence/dataset-eval-20260914")
    ap.add_argument(
        "--semantic",
        action="store_true",
        help="开放域用语义对齐(LLM-judge)评估，弥补词面 token 重叠在中英鸿沟下恒为 0 的结构性盲区",
    )
    ap.add_argument("--refresh-sample", action="store_true")
    ap.add_argument(
        "--limit",
        type=int,
        default=0,
        help="只评测前 N 条（共享缓存前 N 行），用于快速迭代，0=全量",
    )
    ap.add_argument(
        "--llm-endpoint",
        choices=["remote", "local"],
        default="remote",
        help="抽取/判卷 LLM 端点：remote=显式用 .env 的 SiliconFlow 远程强模型（竞赛默认）；"
        "local=本地 LM Studio（无参默认优先级）",
    )
    ap.add_argument(
        "--workers",
        type=int,
        default=8,
        help="并行抽取样本的工作线程数（每个样本独立临时 store，共享线程安全的 HTTP client）；"
        "0/1=串行。远程 LLM 可达时建议 8，可大幅压低墙钟时间。",
    )
    args = ap.parse_args()
    if args.workers is not None and args.workers < 1:
        args.workers = 1
    if args.n_pos + args.n_neg != args.n:
        args.n_neg = max(args.n - args.n_pos, 0)

    print(f"[1/3] 抽样 user-preference-564k n={args.n} pos={args.n_pos} neg={args.n_neg} ...", flush=True)
    cached = None if args.refresh_sample else load_cached_samples(args.cache)
    if cached and not args.hf_source:
        rows, sample_stats = cached
        print(
            f"      cache={args.cache} n={len(rows)} indices={len(sample_stats.get('sampled_indices') or [])}",
            flush=True,
        )
    else:
        local_full = CACHE_DIR / "preference_extractor_564k.jsonl"
        sources = []
        if args.hf_source:
            sources = [args.hf_source]
        elif local_full.exists():
            sources = [str(local_full)]
        else:
            sources = list(HF_JSONL_URLS)
        last_err: Exception | None = None
        rows, sample_stats = [], {}
        for source in sources:
            print(f"      streaming {source}", flush=True)
            try:
                rows, sample_stats = sample_hf_rows(
                    source, n_pos=args.n_pos, n_neg=args.n_neg, seed=args.seed
                )
                last_err = None
                break
            except Exception as exc:
                last_err = exc
                print(f"      失败: {type(exc).__name__}: {exc}", flush=True)
        if last_err is not None:
            raise last_err
        save_cached_samples(args.cache, rows, sample_stats)
        print(f"      wrote {args.cache} n={len(rows)}", flush=True)

    if args.limit and 0 < args.limit < len(rows):
        rows = rows[: args.limit]
        print(f"      [limit] 截断为前 {len(rows)} 条（快速迭代）", flush=True)

    if len(rows) < args.n:
        print(f"      [警告] 只抽到 {len(rows)} 条，少于 {args.n}", flush=True)

    modes = ["rule", "fusion"] if args.mode == "both" else [args.mode]
    llm_client = build_llm_client(args.llm_endpoint)
    semantic_client = llm_client if args.semantic else None
    open_domain = {}
    print("[2/3] 开放域抽取 ...", flush=True)

    def _run_open_mode(mode: str) -> tuple[str, dict]:
        print(f"      mode={mode}", flush=True)
        return mode, extract_batch(
            mode,
            rows,
            semantic=args.semantic,
            client=semantic_client,
            llm_client=llm_client,
            workers=args.workers,
        )

    # mode 之间也并行（rule 瞬时、fusion 走 LLM），进一步压低墙钟时间
    with ThreadPoolExecutor(max_workers=max(1, len(modes))) as _ex:
        for mode, res in _ex.map(_run_open_mode, modes):
            open_domain[mode] = res

    print("[3/3] 封闭键 35 条对照 ...", flush=True)
    closed = {}

    def _run_closed_mode(mode: str) -> tuple[str, dict]:
        closed_path = ROOT / "benchmarks" / "datasets" / "preference_extraction.jsonl"
        report = evaluate_closed_set(
            mode, closed_path, restrict_llm_keys=True, workers=args.workers
        )
        return mode, report

    with ThreadPoolExecutor(max_workers=max(1, len(modes))) as _ex:
        for mode, report in _ex.map(_run_closed_mode, modes):
            closed[mode] = dict(report)
            closed[mode]["pass_85"] = (report.get("sample_exact_match_rate") or 0) >= 0.85

    results = {
        "meta": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "seed": args.seed,
            "n": args.n,
            "n_pos": args.n_pos,
            "n_neg": args.n_neg,
            "modes": modes,
            "dataset": "blackhao0426/user-preference-564k",
            "closed_set": "benchmarks/datasets/preference_extraction.jsonl",
            "metric_scope": (
                "open-domain: sample_ok = empty gold → no extract, else gold-action token "
                "coverage recall>=0.5; not exact condition-action JSON match. "
                "closed-set: exact preference_key=value."
            ),
            "commit": _git_short_head(ROOT),
            "eval_code_commit": "17af2b3f509ecea1ed10f1b02134277e530775ed",
            "workers": args.workers,
            "semantic": bool(args.semantic),
            "llm_endpoint": args.llm_endpoint,
            "llm_model": llm_client.model,
            "llm_base_url": llm_client.base_url,
            "rng": {
                "implementation": "Python random.Random (CPython Mersenne Twister)",
                "seed": args.seed,
                "sampling_fn": "scripts.eval_preference_hf_dataset.sample_hf_rows",
                "algorithm": (
                    "independent Vitter-style reservoir samples: nonempty gold → n_pos, "
                    "empty gold → n_neg; each pool uses rng.randint(0, n_seen-1) replacement"
                ),
            },
            "llm_decode": {
                "temperature": 0.0,
                "top_p": None,
                "top_p_note": "not passed to chat.completions.create; provider default applies",
                "max_tokens_extract": 2000,
                "max_tokens_semantic_judge": 200,
                "prompt_module": "src/preference/prompts.py",
                "extract_prompts": [
                    "EXTRACTION_SYSTEM_PROMPT",
                    "OPEN_DOMAIN_SYSTEM_PROMPT",
                ],
                "judge_prompt_fn": "scripts.eval_preference_hf_dataset._semantic_cover",
            },
        },
        "sample_stats": sample_stats,
        "open_domain": open_domain,
        "closed_set": closed,
    }

    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = out_dir / f"preference_{ts}.json"
    json_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "report": str(json_path),
        # 透明化 fusion 假象：无 LLM key 时 extract_by_llm 静默返回空、退化成 rule，
        # 此时 fusion 与 rule 分数相同并非「fusion 无帮助」，而是 LLM 根本没跑。
        "llm_available": llm_client.available,
        "llm_available_note": (
            "仅表示已配置 API key，不代表网络/服务可达；"
            "若 open_domain.*.semantic_errors>0 说明 LLM 调用失败、相关语义指标不可用。"
        ),
        "sample": {
            "n": len(rows),
            "n_pos": sample_stats.get("n_pos_sampled"),
            "n_neg": sample_stats.get("n_neg_sampled"),
            "n_total_cached": sample_stats.get("n_total_sampled"),
        },
        "open_domain": {
            mode: {
                "sample_accuracy": results["open_domain"][mode]["sample_accuracy"],
                "detection_accuracy": results["open_domain"][mode]["detection_accuracy"],
                "gold_action_recall": results["open_domain"][mode]["gold_action_recall"],
                "semantic_recall": results["open_domain"][mode].get("semantic_recall"),
                "semantic_sample_accuracy": results["open_domain"][mode].get(
                    "semantic_sample_accuracy"
                ),
                "semantic_errors": results["open_domain"][mode].get("semantic_errors"),
                "pass_85": results["open_domain"][mode]["pass_85"],
            }
            for mode in modes
        },
        "closed_set": closed,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n报告: {json_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
