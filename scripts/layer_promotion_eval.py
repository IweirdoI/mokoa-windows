"""Evaluate MemoryLayer promotion SHORT -> MID -> LONG.

构造独立的测试集，验证三层存储的晋级逻辑是否符合预期：

  - SHORT -> MID: 仅 heat >= heat_threshold(默认 3.0) 的短期记忆晋级；
                heat = access_count + quality_score*5 + confidence*1
  - MID -> LONG: 仅 quality_score >= quality_threshold(默认 0.5) 的中期记忆晋级；
                未达标者回退保留在中期。

测试集覆盖：完整晋级、仅到中期不长期、滞留短期、阈值边界、重复访问升温、
跨对话同键合并、空晋级无操作、偏好/知识混合晋级。

用法:
  python scripts/layer_promotion_eval.py
  python scripts/layer_promotion_eval.py --out docs/evidence/layer_promotion_eval.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.common.models import MemoryItem, MemoryType, MemoryLayer
from src.core.layer_manager import LayerManager


def _mk(
    item_id: str,
    content: str,
    mtype: MemoryType,
    *,
    quality: float | None = None,
    confidence: float | None = None,
    scene: str | None = None,
    key: str | None = None,
) -> MemoryItem:
    meta: dict = {}
    if quality is not None:
        meta["quality_score"] = quality
    if confidence is not None:
        meta["confidence"] = confidence
    if key is not None:
        meta["preference_key" if mtype == MemoryType.PREFERENCE else "knowledge_key"] = key
    return MemoryItem(
        id=item_id,
        memory_type=mtype,
        layer=MemoryLayer.SHORT,
        content=content,
        user_id="bench",
        scene_id=scene,
        metadata=meta,
        updated_at=datetime.utcnow(),
    )


def _new_manager() -> LayerManager:
    return LayerManager()  # 默认 heat_threshold=3.0, quality_threshold=0.5


# ---------------------------------------------------------------------------
# 测试用例：每个返回 (name, passed, detail)
# ---------------------------------------------------------------------------


def tc_full_promotion() -> tuple[str, bool, str]:
    """高质量明确记忆应一路晋级到 LONG。"""
    mgr = _new_manager()
    item = _mk("s1", "默认用中文回复", MemoryType.PREFERENCE, quality=0.8, confidence=0.7)
    mgr.write_short(item)
    promoted_mid = mgr.promote_to_mid("bench")
    promote_long, retained = mgr.promote_to_long("bench")
    ok = (
        len(promoted_mid) == 1
        and len(promote_long) == 1
        and len(retained) == 0
        and promote_long[0].layer == MemoryLayer.LONG
        and mgr.mid_count("bench") == 0
        and mgr.long_count("bench") == 1
    )
    return ("完整晋级 SHORT->MID->LONG", ok,
            f"mid={len(promoted_mid)} long={len(promote_long)} retained={len(retained)}")


def tc_mid_not_long() -> tuple[str, bool, str]:
    """heat 达标晋级到 MID，但质量不足应回退保留在 MID。"""
    mgr = _new_manager()
    item = _mk("s2", "偶尔用浅色主题", MemoryType.PREFERENCE, quality=0.4)
    mgr.write_short(item)
    mgr.promote_to_mid("bench")
    promote_long, retained = mgr.promote_to_long("bench")
    ok = (
        len(promote_long) == 0
        and len(retained) == 1
        and retained[0].layer == MemoryLayer.MID
        and mgr.long_count("bench") == 0
        and mgr.mid_count("bench") == 1
    )
    return ("仅到 MID 不长期(质量不足)", ok,
            f"long={len(promote_long)} retained={len(retained)}")


def tc_stays_short() -> tuple[str, bool, str]:
    """heat 不足应滞留 SHORT，不晋级。"""
    mgr = _new_manager()
    item = _mk("s3", "临时查看了设置", MemoryType.KNOWLEDGE, quality=0.2)
    mgr.write_short(item)
    promoted = mgr.promote_to_mid("bench")
    ok = (
        len(promoted) == 0
        and mgr.short_stats("bench").count == 1
        and mgr.mid_count("bench") == 0
    )
    return ("滞留 SHORT(热度不足)", ok,
            f"promoted={len(promoted)} short={mgr.short_stats('bench').count}")


def tc_heat_boundary() -> tuple[str, bool, str]:
    """heat 恰好等于阈值(3.0)应晋级(>=)。"""
    mgr = _new_manager()
    # quality=0.4 -> 1 + 0.4*5 = 3.0
    item = _mk("s4", "默认语言中文", MemoryType.PREFERENCE, quality=0.4)
    mgr.write_short(item)
    promoted = mgr.promote_to_mid("bench")
    ok = len(promoted) == 1 and promoted[0].layer == MemoryLayer.MID
    return ("热度阈值边界(==3.0 晋级)", ok, f"promoted={len(promoted)}")


def tc_quality_boundary() -> tuple[str, bool, str]:
    """quality 恰好等于阈值(0.5)应晋级到 LONG(>=)。"""
    mgr = _new_manager()
    item = _mk("s5", "回复用中文", MemoryType.PREFERENCE, quality=0.5)
    mgr.write_short(item)
    mgr.promote_to_mid("bench")
    promote_long, retained = mgr.promote_to_long("bench")
    ok = len(promote_long) == 1 and promote_long[0].layer == MemoryLayer.LONG
    return ("质量阈值边界(==0.5 长期)", ok,
            f"long={len(promote_long)} retained={len(retained)}")


def tc_touch_raises_heat() -> tuple[str, bool, str]:
    """单次写入 heat 不足，重复访问(touch)升温后晋级。"""
    mgr = _new_manager()
    item = _mk("s6", "常用 D 盘", MemoryType.PREFERENCE, quality=0.3)  # 1+1.5=2.5<3.0
    res = mgr.write_short(item)
    before = mgr.promote_to_mid("bench")
    mgr.touch_short(res.item.id)  # access_count=2 -> 3.5 >= 3.0
    after = mgr.promote_to_mid("bench")
    ok = len(before) == 0 and len(after) == 1 and after[0].layer == MemoryLayer.MID
    return ("重复访问升温后晋级", ok,
            f"before={len(before)} after={len(after)}")


def tc_cross_session_consolidation() -> tuple[str, bool, str]:
    """跨对话同偏好键碎片在晋中期时被固化合并为 1 条。"""
    mgr = _new_manager()
    a = _mk("a1", "用户喜欢中文", MemoryType.PREFERENCE, quality=0.8, key="lang")
    b = _mk("a2", "用户偏好中文", MemoryType.PREFERENCE, quality=0.7, key="lang")
    mgr.write_short(a)
    mgr.write_short(b)
    # 写入时不按偏好键合并(merge_identity_key 只看 record_key/fingerprint) -> 两条短期
    short_before = mgr.short_stats("bench").count
    promoted = mgr.promote_to_mid("bench")
    mid_items = mgr.read_mid("bench")
    merged = any(it.metadata.get("mid_merged") for it in mid_items)
    ok = short_before == 2 and len(promoted) == 1 and len(mid_items) == 1 and merged
    return ("跨对话同键固化合并", ok,
            f"short_before={short_before} mid={len(mid_items)} merged={merged}")


def tc_empty_noop() -> tuple[str, bool, str]:
    """空存储晋级应为无操作且不报错。"""
    mgr = _new_manager()
    mid = mgr.promote_to_mid("bench")
    long_, retained = mgr.promote_to_long("bench")
    ok = mid == [] and long_ == [] and retained == []
    return ("空晋级无操作", ok, "ok" if ok else "fail")


def tc_mixed_types() -> tuple[str, bool, str]:
    """偏好与知识混合，各自按规则独立晋级且类型保留。"""
    mgr = _new_manager()
    mgr.write_short(_mk("p1", "喜欢中文", MemoryType.PREFERENCE, quality=0.8))
    mgr.write_short(_mk("k1", "端口 8080", MemoryType.KNOWLEDGE, quality=0.6))
    mgr.promote_to_mid("bench")
    long_, _ = mgr.promote_to_long("bench")
    types = {it.memory_type for it in long_}
    ok = (
        len(long_) == 2
        and MemoryType.PREFERENCE in types
        and MemoryType.KNOWLEDGE in types
    )
    return ("偏好/知识混合晋级", ok,
            f"long={len(long_)} types={[t.value for t in types]}")


TESTS = [
    tc_full_promotion,
    tc_mid_not_long,
    tc_stays_short,
    tc_heat_boundary,
    tc_quality_boundary,
    tc_touch_raises_heat,
    tc_cross_session_consolidation,
    tc_empty_noop,
    tc_mixed_types,
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    results = []
    passed = 0
    for test in TESTS:
        name, ok, detail = test()
        passed += int(ok)
        results.append({"name": name, "passed": ok, "detail": detail})
        flag = "OK " if ok else "XX "
        print(f"  {flag}{name}  | {detail}")

    total = len(TESTS)
    rate = passed / total if total else 0.0
    print(f"\n晋级正确率: {passed}/{total} = {rate:.0%}")
    report = {
        "samples": total,
        "passed": passed,
        "pass_rate": round(rate, 4),
        "details": results,
    }
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {args.out}")
    if passed != total:
        print("存在未通过用例")


if __name__ == "__main__":
    main()
