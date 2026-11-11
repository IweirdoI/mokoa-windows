"""知识库自然语言精准遗忘解析器。

Owner: 2abac5a6
Ref: 比赛题目 (5) 敏感信息识别与精准遗忘 —— 知识库按自然语言条件批量定位遗忘

本模块把「遗忘知识库中关于某主题的内容」这类自然语言指令解析为一份
**知识遗忘计划（KnowledgeForgetPlan）**：

- ``is_knowledge_forget``：是否命中知识库遗忘意图；
- ``recall_query``：用于定向召回的查询串（从指令抽取的主题词），提升知识命中率；
- ``want_all``：是否为「删除全部某类知识」的提权指令；
- ``memory_types``：知识库支持的细分类（非偏好），用于删除过滤；
- ``filters``：配合cb0aeabe ``delete_memory`` 的批量删除过滤条件（含 ``nl_condition``）。

解析阶段**不执行任何删除**，正式删除由 ``Orchestrator.forget`` 统一编排
（定向召回 → 候选打分 → ``_purge_memory`` 走 ``delete_memory`` 级联删除；
``want_all`` 时直接按 filters 批量删除）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.security.forget_parser import ForgetResolution, ForgetTargetParser


# 知识遗忘意图触发词：在 forget_parser 的 knowledge 意图词基础上，额外显式
# 点名「知识库 / 文档 / 资料库」等存储体，避免与普通记忆/偏好遗忘混淆。
_KNOWLEDGE_TRIGGERS: tuple[str, ...] = (
    "知识库", "知识", "文档", "资料库", "资料", "笔记", "文库",
    "knowledge", "document", "doc", "wiki", "corpus",
)


@dataclass
class KnowledgeForgetPlan:
    """自然语言知识遗忘指令的解析结果（删除前的中枢表示）。"""

    is_knowledge_forget: bool = False
    recall_query: str = ""
    want_all: bool = False
    memory_types: list[str] = field(default_factory=list)
    filters: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转字典，供 /forget/parse 接口与日志展示。"""
        return {
            "is_knowledge_forget": self.is_knowledge_forget,
            "recall_query": self.recall_query,
            "want_all": self.want_all,
            "memory_types": list(self.memory_types),
            "filters": dict(self.filters),
            "note": self.note,
        }


class KnowledgeForgetResolver:
    """把自然语言指令解析为知识库遗忘计划。"""

    # 知识库支持的细分类（见 memory_schema.MemoryTypeTag 非 preference 部分）。
    KNOWLEDGE_TYPES: tuple[str, ...] = (
        "tool_call",
        "document_chunk",
        "project_context",
        "feedback",
    )

    def __init__(self, parser: ForgetTargetParser | None = None) -> None:
        # 复用通用遗忘解析器做类型意图识别、主题抽取与候选打分，
        # 保证知识遗忘与普通记忆/偏好遗忘走同一套安全门控（min_score=0.6）。
        self._parser = parser or ForgetTargetParser()

    def plan(self, instruction: str) -> KnowledgeForgetPlan:
        """解析知识库遗忘指令，返回删除前计划（不执行删除）。"""
        instruction = (instruction or "").strip()
        if not instruction:
            return KnowledgeForgetPlan(note="空指令，未解析知识遗忘计划")

        lowered = instruction.lower()
        has_knowledge_intent = any(w in lowered for w in _KNOWLEDGE_TRIGGERS)

        # 复用 forget_parser 的类型意图识别（preference/knowledge/None）
        type_intent = self._parser._detect_type_intent(instruction)
        # 复用「删除全部」提权词识别
        want_all = self._parser._detect_all(instruction)

        if not has_knowledge_intent and type_intent != "knowledge":
            return KnowledgeForgetPlan(note="非知识库遗忘指令")

        # 抽取主题词作为定向召回查询，提升知识条目命中率
        topics = self._parser._extract_topics(instruction, type_intent)
        recall_query = " ".join(topics)

        memory_types = list(self.KNOWLEDGE_TYPES)
        # filters 交给 Orchestrator 填充真实 user_id；此处仅描述条件形态。
        filters: dict[str, Any] = {
            "user_id": "<PLACEHOLDER_USER_ID>",
            "memory_type": list(memory_types),
            "nl_condition": instruction,
        }

        note = "将按自然语言条件定位并删除知识库内容"
        if want_all:
            note = f"将删除全部知识类记忆（{', '.join(memory_types)}）"

        return KnowledgeForgetPlan(
            is_knowledge_forget=True,
            recall_query=recall_query,
            want_all=want_all,
            memory_types=memory_types,
            filters=filters,
            note=note,
        )

    def resolve(self, instruction: str, candidates: list[Any]) -> ForgetResolution:
        """对知识候选做目标解析（复用通用遗忘解析器，已按 knowledge 意图过滤）。"""
        return self._parser.parse(instruction, candidates)
