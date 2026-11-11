"""意图识别的单词边界匹配回归测试。

防止裸 substring 匹配造成的误路由：
- "capital" 不应因含 "api" 被误判为知识意图
- "debug" 应被正确识别为知识意图（其内部含 "bug" 但整体不是 bug 词）
- "alike"/"likely" 不应因含 "like" 被误判为偏好意图
"""

import pytest

from src.core.recall_ranker import knowledge_intent, preference_intent


@pytest.mark.parametrize("query,expected", [
    ("health benefits of jogging", True),
    ("how does image compression work", True),
    ("how to debug nginx timeout", True),
    ("what is the default port", False),
    ("printer share name", False),
    ("what is my preference for reply language", False),
    ("星河笔记 health check", False),
    ("", False),
])
def test_auto_route_does_not_confuse_general_topics_with_office_queries(query, expected):
    from src.core.recall_ranker import open_domain_semantic
    assert open_domain_semantic(query, []) is expected


@pytest.mark.parametrize(
    "query,expected",
    [
        # 子串误命中防护
        ("capital city of France", False),
        ("alike things happen", False),
        ("the most likely cause", False),
        # 真实知识意图（含英文运维词）
        ("how to debug nginx timeout", True),
        ("configure the database server", True),
        ("the docker container crashes on deploy", True),
        ("install and upgrade the package", True),
        ("排查 wifi 连接超时", True),
        # 中文知识意图
        ("我喜欢把 nginx 配置写成中文注释", True),
    ],
)
def test_knowledge_intent_word_boundary(query, expected):
    assert knowledge_intent(query) is expected


@pytest.mark.parametrize(
    "query,expected",
    [
        # 子串误命中防护
        ("alike things happen", False),
        ("the most likely cause", False),
        # 真实偏好意图
        ("I like pizza", True),
        ("我偏好简洁的中文回复", True),
        ("what is my preference for reply language", True),
    ],
)
def test_preference_intent_word_boundary(query, expected):
    assert preference_intent(query) is expected
