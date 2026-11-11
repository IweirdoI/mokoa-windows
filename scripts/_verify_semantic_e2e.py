"""临时端到端验证：语义层（LM Studio qwen3.5-0.8b）与正则层协同脱敏闭环。

覆盖场景：正则抓不到的带点工号、隐式地址，与正则可抓的手机号混合；
以及无敏感信息文本的负例。验证脱敏/类型/映射/还原/索引加密落盘。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.config import load_default_config  # noqa: E402
from src.security.service import SecurityAndForgetService  # noqa: E402


def main() -> None:
    cfg = load_default_config()
    cfg["semantic_detection"] = {"enabled": True, "min_confidence": 0.6}
    index = tempfile.mktemp(suffix=".json")
    cfg["index_path"] = index
    svc = SecurityAndForgetService.from_config(cfg)
    print("语义检测器:", "启用" if svc.semantic_detector is not None else "未启用")
    print("模型:", svc.semantic_detector._model, "| reasoning:", svc.semantic_detector._reasoning)
    print("=" * 60)

    cases = [
        # 正例：工号（带点，正则抓不到）+ 隐式地址（正则无规则）+ 手机（正则层）
        "我的工号是NO.9527，住在北京市海淀区中关村大街1号，电话13812345678",
        # 负例：无敏感信息
        "这周三是纪念日，记得买花",
    ]
    for text in cases:
        out, detected, mapping = svc.filter_sensitive(text)
        print("原文:", text)
        print("脱敏:", out)
        print("类型:", detected)
        print("映射:", {k: v for k, v in mapping.items()})
        print("还原:", svc.restore_sensitive(out))
        print("-" * 60)

    print("索引统计:", svc.get_sensitive_stats("default"))
    svc.save_index()
    with open(index, encoding="utf-8") as handle:
        raw = handle.read()
    print("落盘明文泄露检查:", "13812345678" in raw, "| NO.9527:", "NO.9527" in raw, "| 中关村:", "中关村" in raw)


if __name__ == "__main__":
    main()
