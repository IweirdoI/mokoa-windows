"""跨语种检索：中文问句不应因词面过滤丢弃英文文档。

Owner: da0f41a1
复现场景：KO-02/03/04/09/10、DOC-01/02 等知识检索 FAIL（词面过滤过严）。
该测试只验证 rerank_and_filter 的重排/过滤逻辑（不依赖真实向量检索），
确认中英主题对齐能够保留相关文档；完整补召链路见 test_recall_strategy.py。
"""

from __future__ import annotations

from src.common.models import MemoryItem, MemoryLayer, MemoryType
from src.core.recall_ranker import rerank_and_filter


def _item(content: str, mtype=MemoryType.KNOWLEDGE) -> MemoryItem:
    return MemoryItem(
        id=f"m-{abs(hash(content)) % 100000}",
        memory_type=mtype,
        layer=MemoryLayer.LONG,
        content=content,
        user_id="u",
        metadata={},
    )


def test_chinese_query_keeps_english_doc() -> None:
    en = _item("Printer sharing name is XEROX-Share. The default port is 8080.")
    zh = _item("用户偏好用中文回复。")
    result = rerank_and_filter("打印机共享名是什么", [en, zh], top_k=5)
    assert en in result, "英文文档应作为跨语种项被保留"


def test_chinese_query_drops_unrelated_same_language_noise() -> None:
    en = _item("Printer sharing name is XEROX-Share. The default port is 8080.")
    zh_rel = _item("打印设置里可以改共享名。")
    zh_noise = _item("我喜欢用 Python 写代码。")
    result = rerank_and_filter("打印机共享名是什么", [en, zh_rel, zh_noise], top_k=5)
    assert en in result, "英文文档应被跨语种保留"
    assert zh_rel in result, "有词面命中的同语种项应保留"
    assert zh_noise not in result, "无词面命中的同语种噪声应被过滤"


def test_no_signal_non_knowledge_returns_empty() -> None:
    items = [_item("The weather is sunny today.")]
    # 「讲个笑话」无知识/偏好意图提示，且无词面信号 → 返回空，不回退噪声近邻
    result = rerank_and_filter("讲个笑话", items, top_k=5)
    assert result == []


def test_default_port_uses_knowledge_topic_evidence() -> None:
    en = _item("The default port configured in the manual is 8080.")
    items = [en]
    # 默认不是偏好；端口/手册通过主题同义词对齐英文证据。
    result = rerank_and_filter("手册里的默认端口是多少", items, top_k=5)
    assert en in result


def test_mixed_intent_query_still_filters_by_overlap() -> None:
    # 混合意图：既含知识词（nginx/配置）又含偏好词（我喜欢）。修复前该分支跳过词面
    # 过滤，导致无关偏好项也被保留、相关性静默劣化。
    relevant = _item("nginx 的默认配置路径是 /etc/nginx/nginx.conf")
    noise = _item("今天天气晴朗，适合去公园散步。")
    result = rerank_and_filter(
        "我喜欢把 nginx 配置写成中文注释", [relevant, noise], top_k=5
    )
    assert relevant in result, "有词面命中的知识项应保留"
    assert noise not in result, "无词面重叠的偏好噪声应被过滤"
