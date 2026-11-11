"""P0-B: compare rule-only vs LLM-only on 测试用例 samples.

CMD:
  set SILICONFLOW_API_KEY=sk-...
  python scripts\\pref_rule_vs_llm.py
  python scripts\\pref_rule_vs_llm.py --debug   # print raw LLM on empty
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.preference.store import PreferenceMemoryStore

SAMPLES = [
    ("1", "帮我把这个文件从桌面挪到 D 盘项目文件夹", {"habit.download_path", "habit.file_naming_rule"}),
    ("2", "帮我把这周的日报整理成表格发给我", {"tool_selection.data_processing_tool"}),
    ("3", "把桌面上所有 .tmp 文件删了，直接删不用问我", {"security.confirm_before_rm", "workflow.preview_before_execute"}),
    ("4", "帮我搜一下采购合同，上周改过的那个", {"tool_selection.file_search_tool"}),
    ("5", "用英文给我出一份这个 Excel 的分析报告", {"style.report_format", "style.reply_language"}),
    ("6", "把 downloads 按类型分类放到不同文件夹", {"habit.file_organize_rule", "workflow.backup_before_modify"}),
    ("7", "把这个目录打个压缩包，密码设一下", {"habit.archive_format", "security.confirm_before_highrisk"}),
    ("8", "帮我把这个 Word 转换成 PDF 发给客户", {"style.report_format", "workflow.auto_complete_steps"}),
    ("9", "帮我批量重命名这些图片，按日期顺序", {"habit.file_naming_rule", "tool_selection.batch_operation_tool"}),
    ("10", "把这份合同里的日期都改成 2026-08-01 格式", {"tool_selection.text_processing_tool", "workflow.backup_before_modify"}),
]


def keys_of(store: PreferenceMemoryStore, text: str, **kwargs) -> set[str]:
    entries = store.extract_from_text(text, user_id="cmp", scene_id=None, **{})
    # extract_from_text doesn't pass debug; call extract path via store fields
    return {e.metadata["preference_key"] for e in entries}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--fusion",
        action="store_true",
        help="also score production mode: use_llm=True + allow_rules=True",
    )
    args = parser.parse_args()

    rule_store = PreferenceMemoryStore(
        storage_dir=ROOT / "data" / "preferences_cmp_rule",
        use_llm=False,
        allow_rules=True,
    )
    llm_store = PreferenceMemoryStore(
        storage_dir=ROOT / "data" / "preferences_cmp_llm",
        use_llm=True,
        allow_rules=False,
    )
    fusion_store = PreferenceMemoryStore(
        storage_dir=ROOT / "data" / "preferences_cmp_fusion",
        use_llm=True,
        allow_rules=True,
    )

    header = f"{'#':<3} {'rule':<5} {'llm':<5}"
    if args.fusion:
        header += f" {'fuse':<5}"
    header += "  missing_llm / extra_llm"
    print(header)
    print("-" * 78)
    llm_ok = 0
    fuse_ok = 0
    for sid, text, expected in SAMPLES:
        rule_keys = {
            e.metadata["preference_key"]
            for e in rule_store.extract_from_text(text, user_id=f"r{sid}")
        }
        if args.debug:
            from uuid import uuid4

            from src.common.models import DataSource, RawRecord
            from src.preference.extractor import extract_preferences

            rec = RawRecord(
                id=str(uuid4()),
                source=DataSource.CONVERSATION,
                user_id=f"l{sid}",
                scene_id=None,
                payload={"content": text},
            )
            prefs = extract_preferences([rec], use_llm=True, allow_rules=False, debug=True)
            llm_keys = {f"{p.category}.{p.key}" for p in prefs}
            llm_store.extract_from_text(text, user_id=f"l{sid}")
        else:
            llm_keys = {
                e.metadata["preference_key"]
                for e in llm_store.extract_from_text(text, user_id=f"l{sid}")
            }

        miss = expected - llm_keys
        extra = llm_keys - expected
        hit_rule = "Y" if expected <= rule_keys else "N"
        hit_llm = "Y" if expected <= llm_keys else "N"
        if hit_llm == "Y":
            llm_ok += 1

        line = f"{sid:<3} {hit_rule:<5} {hit_llm:<5}"
        if args.fusion:
            fuse_keys = {
                e.metadata["preference_key"]
                for e in fusion_store.extract_from_text(text, user_id=f"f{sid}")
            }
            hit_fuse = "Y" if expected <= fuse_keys else "N"
            if hit_fuse == "Y":
                fuse_ok += 1
            line += f" {hit_fuse:<5}"
        line += f"  miss={sorted(miss) or '-'} extra={sorted(extra) or '-'}"
        print(line)
        print(f"    text: {text}")
        print(f"    rule: {sorted(rule_keys)}")
        print(f"    llm : {sorted(llm_keys)}")
        if args.fusion:
            print(f"    fuse: {sorted(fuse_keys)}")

    print("-" * 78)
    print(f"LLM-only expected-cover: {llm_ok}/{len(SAMPLES)} (target >= 8/10)")
    if args.fusion:
        print(f"Fusion  expected-cover: {fuse_ok}/{len(SAMPLES)} (production default)")
    return 0 if llm_ok >= 8 else 1



if __name__ == "__main__":
    raise SystemExit(main())
