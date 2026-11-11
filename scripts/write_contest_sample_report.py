#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a minute-stamped contest sampling report from eval JSON artifacts."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CONTEST = {
    "preference_acc": 0.85,
    "recall": 0.85,
    "latency_ms": 500,
    "conflict_acc": 0.88,
}


def _latest(out: Path, prefix: str) -> Path | None:
    files = sorted(out.glob(f"{prefix}_*.json"))
    return files[-1] if files else None


def _load(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _git(cmd: list[str]) -> str:
    try:
        return subprocess.check_output(cmd, cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def _mark(ok: bool | None) -> str:
    if ok is True:
        return "过线"
    if ok is False:
        return "未过线"
    return "—"


def _hit(block: dict | None, key: str = "hit_at_5"):
    if not block:
        return None
    return block.get(key)


def _rel(path: Path | None) -> str:
    if path is None:
        return "**缺失**"
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stamp", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    out = ROOT / args.out
    out.mkdir(parents=True, exist_ok=True)

    pref_p = _latest(out, "preference")
    conf_p = _latest(out, "conflict_longmemeval")
    tool_p = _latest(out, "dialog_tool")
    beh_p = _latest(out, "behavior_knowledge")
    quant_p = _latest(out, "dialog_quant")
    ret_p = _latest(out, "retrieval")

    pref = _load(pref_p)
    conf = _load(conf_p)
    tool = _load(tool_p)
    beh = _load(beh_p)
    quant = _load(quant_p)
    ret = _load(ret_p)
    snap = _load(out / "reproducibility.json")
    raw_manifest = _load(out / "raw_io" / "manifest.json")

    head = _git(["git", "rev-parse", "HEAD"])
    head_short = _git(["git", "rev-parse", "--short", "HEAD"])
    status = _git(["git", "status", "--short"])

    od = (pref.get("open_domain") or {}).get("fusion") or (pref.get("open_domain") or {}).get("rule") or {}
    closed_f = (pref.get("closed_set") or {}).get("fusion") or {}
    closed_r = (pref.get("closed_set") or {}).get("rule") or {}
    meta = pref.get("meta") or {}
    sample_stats = pref.get("sample_stats") or {}

    marco = ret.get("msmarco") or {}
    marco_qm = marco.get("query_memory") or {}
    marco_orch = marco.get("orchestrator_recall") or {}
    marco_sem = marco.get("orchestrator_semantic") or {}
    trec = ret.get("trec") or {}
    trec_qm = trec.get("query_memory") or {}
    trec_orch = trec.get("orchestrator_recall") or {}
    trec_sem = trec.get("orchestrator_semantic") or {}

    e2e = conf.get("e2e") or {}
    resolver = conf.get("resolver") or {}
    conf_meta = conf.get("meta") or {}

    mwoz_hit = _hit((tool.get("multiwoz") or {}).get("retrieval"))
    tb_hit = _hit((tool.get("toolbench") or {}).get("retrieval"))
    dd_hit = _hit((quant.get("dailydialog") or {}).get("retrieval"))
    db_hit = _hit((quant.get("dialogbench") or {}).get("retrieval"))
    pc = beh.get("personachat") or {}
    pc_fact_hit = _hit(
        pc.get("retrieval_persona_facts") or pc.get("retrieval_persona_query_on_facts")
    )
    pc_concat_hit = _hit(
        pc.get("retrieval_concat_persona_query")
        or pc.get("retrieval_concat")
        or pc.get("retrieval_persona_query_on_concat")
    )

    pref_sem = od.get("semantic_sample_accuracy")
    pref_token = od.get("sample_accuracy")
    pref_pass = od.get("pass_85")
    closed_f_rate = closed_f.get("sample_exact_match_rate")
    closed_r_rate = closed_r.get("sample_exact_match_rate")
    marco_r5 = marco_qm.get("recall_at_5")
    marco_p95 = marco_orch.get("query_p95_ms") or marco_qm.get("query_p95_ms")
    conflict_acc = e2e.get("correctness") if e2e and not e2e.get("skipped") else resolver.get("correctness")
    conflict_p95 = e2e.get("query_p95_ms")
    n_dialog = (tool.get("meta") or {}).get("n") or 150
    nq = (ret.get("meta") or {}).get("nq") or 150

    llm = (snap.get("llm") or {})
    sampling = (snap.get("sampling") or {})
    project = (snap.get("project") or {})

    pref_vs_85 = None if pref_sem is None else pref_sem >= CONTEST["preference_acc"]
    closed_f_vs = None if closed_f_rate is None else closed_f_rate >= CONTEST["preference_acc"]
    marco_vs = None if marco_r5 is None else marco_r5 >= CONTEST["recall"]
    lat_vs = None if marco_p95 is None else marco_p95 <= CONTEST["latency_ms"]
    conf_vs = None if conflict_acc is None else conflict_acc >= CONTEST["conflict_acc"]

    lines = []
    a = lines.append
    a(f"# 评选标准 git 数据集抽测报告 `{args.stamp}`")
    a("")
    a("对照赛题 **六、作品评选标准**（3）性能指标四条硬线，在提交 `17af2b3`（评测并行化 + 封闭键 fusion 限制已知键）上做可复现抽测。")
    a("")
    a(f"- 生成时间：{datetime.now().isoformat(timespec='seconds')}")
    a(f"- 证据目录：`{_rel(out)}`")
    a(f"- 原始输入：`{_rel(out / 'raw_io')}`")
    a(f"- 可复现快照：`{_rel(out / 'reproducibility.json')}`")
    a("")
    a("## 1. 与赛题四条硬指标对照")
    a("")
    a("| 指标 | 赛题最低 | 本轮主口径 | 判定 |")
    a("|---|---:|---|---|")
    a(
        f"| 偏好提取准确率 | ≥85% | 开放域 fusion+语义 **{pref_sem}**"
        f"（token {pref_token}）；封闭键 fusion **{closed_f_rate}** / rule **{closed_r_rate}** | "
        f"{_mark(bool(pref_pass) if pref_pass is not None else pref_vs_85)} |"
    )
    a(
        f"| 知识检索召回率 | ≥85% | MS MARCO Recall@5 **{marco_r5}**"
        f"（nq={nq} / rc={(ret.get('meta') or {}).get('rc')} 抽样，非 8.8M） | "
        f"{_mark(marco_vs)} |"
    )
    a(
        f"| 知识检索响应时间 | ≤500ms | MS MARCO 编排 P95 **{marco_p95}ms**；"
        f"TREC 编排 P95 **{trec_orch.get('query_p95_ms')}ms** | "
        f"{_mark(lat_vs)} |"
    )
    a(
        f"| 知识冲突处理正确率 | ≥88% | latest_wins / resolver **{conflict_acc}** "
        f"（n={e2e.get('samples') or resolver.get('samples')}）；P95 {conflict_p95}ms | "
        f"{_mark(conf_vs)} |"
    )
    a("")
    a("主口径说明：")
    a("")
    a("- 偏好开放域以 `--semantic` LLM-judge 样本准确率为过线判据（金标是自由文本 condition/action，系统是 key=value）。")
    a("- 知识召回对外写抽样 **MS MARCO Recall@5**，不用 TREC Recall@5 或对话自检索 Hit@5 去平均。")
    a("- 冲突正确率用 resolver + latest_wins，不用金标 QA 子串。")
    a("- 延迟用 MS MARCO 默认编排 P95；TREC 为 pid 子库，不是官方 8.8M。")
    a("")
    a("## 2. 可复现变量")
    a("")
    a("### 2.1 项目版本")
    a("")
    a("| 变量 | 值 |")
    a("|---|---|")
    a(f"| 工作区 HEAD | `{project.get('workspace_head') or head}`（`{head_short}`） |")
    a("| 评测/产品代码基线 | `17af2b3f509ecea1ed10f1b02134277e530775ed` |")
    a("| 基线说明 | `perf(preference): 评测脚本并行化 + 修复封闭键 fusion 过线` |")
    a(f"| origin/master | `{project.get('origin_master') or _git(['git', 'rev-parse', 'origin/master'])}` |")
    a(f"| extractor.py SHA256 | `{project.get('extractor_sha256')}` |")
    a(f"| store.py SHA256 | `{project.get('store_sha256')}` |")
    a(f"| prompts.py SHA256 | `{project.get('prompts_sha256')}` |")
    a(f"| eval_preference_hf_dataset.py SHA256 | `{project.get('eval_preference_sha256')}` |")
    a("")
    a("### 2.2 抽样")
    a("")
    a("| 变量 | 值 |")
    a("|---|---|")
    a("| RNG | `random.Random(seed)`（CPython MT19937） |")
    a(f"| seed | **{sampling.get('seed', 42)}** |")
    a("| 脚本历史默认 n | 50（对话/检索问句） |")
    a("| 对外抽测下限（历史材料） | 100 |")
    a(f"| 本轮 n | **{sampling.get('n', n_dialog)}** = 1.5×100 = 3.0×50（落在 1.5×–5×） |")
    a(f"| 本轮 nq / rc | {sampling.get('nq', nq)} / {sampling.get('rc', 2000)} |")
    a(f"| 偏好 n_pos / n_neg | {sampling.get('n_pos', 120)} / {sampling.get('n_neg', 30)} |")
    a("| 开放域抽样函数 | `scripts.eval_preference_hf_dataset.sample_hf_rows` |")
    a("| 开放域算法 | 正/负例独立 Vitter reservoir，`rng.randint(0, n_seen-1)` 替换 |")
    a("| 对话/检索抽样函数 | `benchmarks.run_dataset_eval.sample_indices` = `sorted(rng.sample(range(total), n))` |")
    a("| 对话 RNG 消耗顺序 | MultiWOZ→ToolBench 共用一个 Random；PersonaChat→BPMN 共用；DailyDialog→DIALOGBENCH 共用 |")
    a("| 冲突抽样 | knowledge-update 按文件顺序取前 n_update（无 rng）；对照 `rng.sample` |")
    a("| TREC 问句 | `trec_nq=0` 用全部 judged qid，不抽样 |")
    a("| TREC 干扰段 | `collect_collection_subset` Vitter reservoir，seed=42，rc=2000 |")
    a("")
    a("抽中条目见 `raw_io/manifest.json` 的 `sampled_indices` / `eval_qids` / `update_ids`。")
    a("")
    a("### 2.3 模型与解码")
    a("")
    a("| 变量 | 值 |")
    a("|---|---|")
    a("| provider | SiliconFlow OpenAI-compatible |")
    a(f"| 请求 model | `{llm.get('model_requested') or meta.get('llm_model')}` |")
    a(f"| 返回 model_version | `{llm.get('model_version')}` |")
    a(f"| system_fingerprint | `{llm.get('system_fingerprint')}` |")
    a(f"| base_url | `{llm.get('base_url') or meta.get('llm_base_url')}` |")
    a(f"| endpoint flag | `{meta.get('llm_endpoint') or 'remote'}` |")
    a(f"| .env 文件中的 SILICONFLOW_LLM_MODEL | `{llm.get('env_file_model')}` |")
    a("| 本轮覆盖 | 评测进程 export `SILICONFLOW_LLM_MODEL=deepseek-ai/DeepSeek-V4-Flash`（对齐 17af2b3 验证） |")
    a("| temperature | **0.0**（抽取与语义判卷） |")
    a("| top_p | 未传，走接口默认 |")
    a("| seed（API） | 未传；靠 temperature=0.0 |")
    a("| max_tokens 抽取 | 2000 |")
    a("| max_tokens 判卷 | 200 |")
    a(f"| workers | {meta.get('workers') or sampling.get('workers') or 8} |")
    a("| 提示词 | `src/preference/prompts.py`（副本 `prompts/preference_prompts.py`） |")
    a("| 判卷提示 | `scripts/eval_preference_hf_dataset.py::_semantic_cover` |")
    a("| 密钥 | 未写入本报告；仅进程内读取 `.env` 的 `SILICONFLOW_API_KEY` |")
    a("")
    a("### 2.4 Embedding / 运行时")
    a("")
    emb = snap.get("embedding") or {}
    a("| 变量 | 值 |")
    a("|---|---|")
    a(f"| Embedding | `{emb.get('OS_AGENT_EMBEDDING_BACKEND') or os.environ.get('OS_AGENT_EMBEDDING_BACKEND', 'kylin')}` |")
    a(f"| Memory backend | `{emb.get('OS_AGENT_MEMORY_BACKEND') or os.environ.get('OS_AGENT_MEMORY_BACKEND', 'memory')}` |")
    a(f"| KYLIN_STRICT | `{emb.get('OS_AGENT_KYLIN_STRICT') or os.environ.get('OS_AGENT_KYLIN_STRICT', '1')}` |")
    a(f"| 向量库 so | `{emb.get('OS_AGENT_KYLIN_VECTOR_LIBRARY')}` |")
    a(f"| so SHA256 | `{emb.get('kylin_so_sha256')}` |")
    a(f"| 向量引擎 endpoint | `{emb.get('OS_AGENT_KYLIN_VECTOR_ENDPOINT')}` |")
    a(f"| Python | `{(snap.get('runtime') or {}).get('python', '').splitlines()[0] if snap else 'host 3.12'}` |")
    a("| 启动 | `scripts/run_contest_git_sample.sh` → `run_host_py.sh` |")
    a("")
    a("### 2.5 评测时工作区")
    a("")
    a("```")
    a((project.get("status_short") or status or "(clean)")[:4000])
    a("```")
    a("")
    a("## 3. 分库抽测")
    a("")
    a("| 数据集 | n | 抽样 | 主数字 | 相对赛题线 |")
    a("|---|---:|---|---|---|")
    a(
        f"| user-preference-564k 开放域 | {od.get('samples') or meta.get('n')} "
        f"（pos={meta.get('n_pos')} neg={meta.get('n_neg')}） | "
        f"sample_hf_rows seed={meta.get('seed')} | "
        f"fusion 语义 {pref_sem}；token {pref_token}；detection {od.get('detection_accuracy')} | "
        f"{_mark(pref_vs_85)} |"
    )
    a(
        f"| preference_extraction.jsonl 封闭键 | 35（全集） | 不抽样 | "
        f"fusion exact {closed_f_rate}；rule exact {closed_r_rate} | "
        f"fusion {_mark(closed_f_vs)}；rule {_mark(None if closed_r_rate is None else closed_r_rate >= 0.85)} |"
    )
    a(
        f"| LongMemEval oracle | {conf_meta.get('n_eval')}（请求 {conf_meta.get('n_requested')}） | "
        f"update={conf_meta.get('n_update')} control={conf_meta.get('n_control')} | "
        f"resolver {resolver.get('correctness')}；e2e {e2e.get('correctness')}；QA子串 {e2e.get('gold_qa_substring_rate')} | "
        f"{_mark(conf_vs)} |"
    )
    a(f"| MultiWOZ | {n_dialog} | sample_indices 共享 rng | Hit@5 {mwoz_hit} | 自检索，非槽位准确率 |")
    a(f"| ToolBench | {n_dialog} | 接 MultiWOZ 之后的同一 rng | Hit@5 {tb_hit} | 自检索，非工具选择 |")
    a(f"| PersonaChat 人设句 / concat | {n_dialog} | sample_indices | {pc_fact_hit} / {pc_concat_hit} | BPMN 已删除则 skipped |")
    a(f"| DailyDialog | {n_dialog} | sample_indices 共享 rng | Hit@5 {dd_hit} | 自检索，非 BLEU |")
    a(f"| DIALOGBENCH | {n_dialog} | 接 DailyDialog 之后 | Hit@5 {db_hit} | 自检索，非选项准确率 |")
    a(
        f"| MS MARCO v1.1 val | nq={nq} / rc={(ret.get('meta') or {}).get('rc')} | "
        f"_sample_indices seed=42 | Recall@5 {marco_r5}；"
        f"query_memory P95 {marco_qm.get('query_p95_ms')}ms；编排 P95 {marco_p95}ms | "
        f"召回 {_mark(marco_vs)}；延迟 {_mark(lat_vs)} |"
    )
    a(
        f"| TREC DL 2019/2020 | {trec.get('n_queries_evaluated') or trec_qm.get('n_queries_evaluated') or len(trec.get('eval_qids') or [])} 问 / "
        f"{trec.get('corpus_size')} 段 | judged 全集 + reservoir 干扰 | "
        f"Recall@5 {trec_qm.get('recall_at_5')}；nDCG@10 {trec_qm.get('ndcg_at_10_graded') or trec_qm.get('ndcg_at_10')}；"
        f"编排 P95 {trec_orch.get('query_p95_ms')}ms | Recall 不可横比 85%；延迟对照 ≤500ms |"
    )
    a("")
    a("开放域偏好细节：")
    a("")
    a(f"- `semantic_recall` = {od.get('semantic_recall')}；`semantic_errors` = {od.get('semantic_errors')}")
    a(f"- `gold_action_recall`（token）= {od.get('gold_action_recall')}；`pass_85_token` = {od.get('pass_85_token')}")
    a(f"- 抽样池：corpus nonempty={sample_stats.get('n_nonempty_in_corpus')} empty={sample_stats.get('n_empty_in_corpus')} source=`{sample_stats.get('source')}`")
    a(f"- 封闭键 fusion LLM 调用：`{closed_f.get('llm_calls')}`")
    a("")
    a("## 4. 原始输入与输出")
    a("")
    a("| 位置 | 内容 |")
    a("|---|---|")
    a("| `raw_io/manifest.json` | 各库 `sampled_indices` / 抽样函数 / 种子 |")
    a("| `raw_io/*.jsonl` | 抽中样本的输入文本（长文本截到 8000 字并标记 truncated） |")
    a("| `raw_io/eval_json_copied/` | 本轮评测 JSON 副本 |")
    a("| `preference_*.json` → `open_domain.*.details` / `closed_set.*.details` | 每条 input、gold、系统抽取 got、语义覆盖 |")
    a("| `conflict_*.json` → `e2e.details` / `resolver.details` | 完整 question/answer/old/new 与判定 |")
    a("| `dialog_*.json` → `retrieval.details` | 每条 query、ranked_ids、hit、latency |")
    a("| `retrieval_*.json` → `*.details` | MS MARCO / TREC 每问 query、召回、延迟、ranked 预览 |")
    a("| `retrieval_*.json` → `trec.subset.distractor_pids` | TREC 2000 条干扰 pid |")
    a("")
    a("## 5. 口径、未测与优化方向")
    a("")
    a("- 知识召回对外写 **抽样 MS MARCO Recall@5**。TREC Recall@5 因每问正例很多、top5 物理上限，不能横比 85%。")
    a("- TREC 为 judged 正例 pid + 2000 随机干扰，**不是**官方 8.8M nDCG。")
    a("- 开放域偏好以语义判卷为公平口径；token 覆盖仅参考。")
    a("- 封闭键 fusion 本轮 `restrict_llm_to_known_keys=True`（仅评测，产品默认关）。")
    a("- 对话 Hit@5 是记忆 id 自检索，不是 MultiWOZ 槽位 / ToolBench 工具选择 / DailyDialog BLEU。")
    a("- 未测：HTTP `/ingest`+`/recall`、麒麟持久化集合、564k 全量、TREC 8.8M 全库、BPMN、官方 LLM-judge。")
    a("- 封闭键全集只有 35 条，无法按 1.5× 扩样本；已用全集。")
    a("- TREC 第一次编码 9707 段时麒麟 embedding 超时（`Timeout was reached`），已用同一 seed/rc 重跑成功（`retrieval_20260915-140209.json`），合并进 `retrieval_20260915-140210.json`。MS MARCO 数字仍来自第一次成功的 `retrieval_20260915-134740.json`。")
    a("")
    a("## 6. 如何复现")
    a("")
    a("```bash")
    a("export OS_AGENT_EMBEDDING_BACKEND=kylin OS_AGENT_MEMORY_BACKEND=memory OS_AGENT_KYLIN_STRICT=1")
    a("export OS_AGENT_KYLIN_VECTOR_LIBRARY=$PWD/build/kylin-vector/libmokoa_kylin_vector_bridge.so")
    a("export OS_AGENT_KYLIN_VECTOR_ENDPOINT=unix:/tmp/kylin-ai-vector-engine-1000.sock")
    a("export SILICONFLOW_LLM_MODEL=deepseek-ai/DeepSeek-V4-Flash")
    a("CONTEST_EVAL_STAMP=" + args.stamp + " bash scripts/run_contest_git_sample.sh")
    a("```")
    a("")
    a("核对 `raw_io/manifest.json` 的 `sampled_indices` 与 `reproducibility.json`。同一 seed/n/抽样函数应得到同一抽样子集。")
    a("偏好缓存为 `git/user-preference-564k/sampled_seed42_n150.jsonl`（gitignore，不进仓库；本轮副本在 `raw_io/`）。")
    a("")
    a("## 7. 证据文件")
    a("")
    a("| 文件 | 用途 |")
    a("|---|---|")
    for label, path in [
        ("preference", pref_p),
        ("conflict", conf_p),
        ("dialog_tool", tool_p),
        ("behavior", beh_p),
        ("dialog_quant", quant_p),
        ("retrieval", ret_p),
        ("reproducibility", out / "reproducibility.json"),
        ("raw_io/manifest", out / "raw_io" / "manifest.json"),
    ]:
        a(f"| {label} | `{_rel(path) if path and Path(path).exists() else '缺失'}` |")
    a("")

    report_path = out / f"测试报告_{args.stamp}.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # also drop a copy at docs/ with the same minute stamp
    docs_copy = ROOT / "docs" / f"评选标准抽测报告_{args.stamp}.md"
    docs_copy.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {report_path}", flush=True)
    print(f"wrote {docs_copy}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
