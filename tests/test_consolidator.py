"""Tests for cross-conversation consolidation (MemoryConsolidator + LayerManager wiring).

26a9835a方向：短期→中期语义聚类合并、中期→长期 LLM 抽象（参考 MemoryOS updater.py /
TiMem）。这些用例刻意走 identity（preference_key）合并路径，不依赖 embedding 是否就绪，
保证在无 GPU / 本地 fallback 环境下也稳定可跑。
"""

from __future__ import annotations

from src.common.models import MemoryItem, MemoryLayer, MemoryType
from src.core.consolidator import MemoryConsolidator
from src.core.layer_manager import LayerManager


def _item(
    user_id,
    content,
    *,
    mtype=MemoryType.PREFERENCE,
    mid=None,
    key=None,
    scene_id=None,
    fingerprint=None,
):
    meta = {}
    if key:
        meta["preference_key"] = key
    if fingerprint:
        meta["fingerprint"] = fingerprint
    return MemoryItem(
        id=mid or f"id-{abs(hash(content))}-{user_id}",
        user_id=user_id,
        content=content,
        memory_type=mtype,
        scene_id=scene_id,
        layer=MemoryLayer.SHORT,
        metadata=meta,
    )


def test_consolidate_short_to_mid_merges_same_identity():
    items = [
        _item("u1", "I like Python", key="lang"),
        _item("u1", "I prefer Python actually", key="lang"),
    ]
    cons = MemoryConsolidator()
    reps, removed = cons.consolidate_short_to_mid(items)
    assert len(reps) == 1
    # 代表项被标记合并，且原始两条都从短期池移除
    assert reps[0].metadata.get("mid_merged") is True
    assert len(removed) == 2
    assert {items[0].id, items[1].id}.issubset(set(removed))


def test_consolidate_short_to_mid_keeps_distinct_identities():
    # 不同身份键 + 高阈值：仅身份合并，语义相近的自由文本不被误并
    items = [
        _item("u1", "I like Python", key="lang"),
        _item("u1", "I like tea", key="drink"),
    ]
    cons = MemoryConsolidator(similarity_threshold=0.99)
    reps, removed = cons.consolidate_short_to_mid(items)
    assert len(reps) == 2
    assert len(removed) == 2


def test_same_preference_key_merges_despite_distinct_fingerprints():
    items = [
        _item("u1", "请用英文回复我", key="style.reply_language", fingerprint="fp-en"),
        _item("u1", "还是用中文回复吧", key="style.reply_language", fingerprint="fp-zh"),
    ]
    cons = MemoryConsolidator(similarity_threshold=0.99)
    reps, removed = cons.consolidate_short_to_mid(items)
    assert len(reps) == 1
    assert reps[0].metadata.get("mid_merged") is True
    assert len(removed) == 2


def test_unrelated_short_english_stays_unmerged():
    items = [
        _item("u1", "hot", mtype=MemoryType.KNOWLEDGE, fingerprint="fp-hot"),
        _item("u1", "cold", mtype=MemoryType.KNOWLEDGE, fingerprint="fp-cold"),
    ]
    cons = MemoryConsolidator(similarity_threshold=0.7)
    reps, removed = cons.consolidate_short_to_mid(items)
    assert len(reps) == 2
    assert all(not r.metadata.get("mid_merged") for r in reps)


class _FakeLLM:
    def __init__(self) -> None:
        self.calls = 0

    def chat_completion(self, messages, temperature=0.0, max_tokens=200):
        self.calls += 1
        return "统一偏好：用户以 Python 为主力语言。"


def test_abstract_mid_to_long_uses_llm_when_enabled():
    items = [
        _item("u1", "I like Python", key="lang"),
        _item("u1", "I prefer Python", key="lang"),
    ]
    llm = _FakeLLM()
    cons = MemoryConsolidator(use_llm=True, llm_client=llm)
    out = cons.abstract_mid_to_long(items)
    assert len(out) == 1
    assert out[0].metadata.get("abstracted") is True
    assert "Python" in out[0].content
    assert llm.calls >= 1


def test_abstract_mid_to_long_no_llm_keeps_items():
    items = [_item("u1", "I like Python", key="lang")]
    cons = MemoryConsolidator(use_llm=False)
    out = cons.abstract_mid_to_long(items)
    assert len(out) == 1
    assert out[0].metadata.get("abstracted") is None


def test_layer_manager_promote_to_mid_consolidates():
    mgr = LayerManager(
        short_capacity=10,
        mid_capacity=50,
        heat_threshold=0.0,
        similarity_threshold=0.7,
    )
    a = _item("u1", "I like Python", key="lang")
    b = _item("u1", "I prefer Python", key="lang")
    mgr.write_short(a)
    mgr.write_short(b)
    promoted = mgr.promote_to_mid("u1")
    # 同身份跨对话碎片被合并成一条中期记忆
    assert len(promoted) == 1
    assert mgr.mid_count("u1") == 1


def test_layer_manager_promote_to_long_abstracts_when_llm_enabled():
    llm = _FakeLLM()
    cons = MemoryConsolidator(use_llm=True, llm_client=llm)
    mgr = LayerManager(
        short_capacity=10,
        mid_capacity=50,
        heat_threshold=0.0,
        quality_threshold=0.0,
        similarity_threshold=0.7,
        consolidator=cons,
    )
    a = _item("u1", "I like Python", key="lang")
    a.metadata["quality_score"] = 0.9
    b = _item("u1", "I prefer Python", key="lang")
    b.metadata["quality_score"] = 0.9
    mgr.write_short(a)
    mgr.write_short(b)
    mgr.promote_to_mid("u1")
    promoted_long, _ = mgr.promote_to_long("u1")
    assert len(promoted_long) == 1
    assert promoted_long[0].metadata.get("abstracted") is True
