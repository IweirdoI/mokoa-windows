"""Abstract interfaces - all modules implement these contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from typing import TYPE_CHECKING

from src.common.models import (
    MemoryItem,
    MemoryLayer,
    RawRecord,
    RetrievalQuery,
    RetrievalResult,
    SensitiveEntry,
)

# MemoryEntry lives in memory_service schema (cb0aeabe); preference extract returns it.
if TYPE_CHECKING:
    from src.knowledge.memory_service.memory_schema import MemoryEntry
    from src.security.forget_parser import ForgetResolution


class SensitiveService(ABC):
    """敏感信息处理服务接口。
    
    Owner: 2abac5a6
    Ref: DLP, PII detection, NL-driven forgetting
    """
    
    @abstractmethod
    def filter_sensitive(self, text: str) -> tuple[str, list[str], dict[str, str]]:
        """检测并替换文本中的敏感信息。
        
        Args:
            text: 原始文本
            
        Returns:
            (脱敏后文本, 检测到的类型列表, 占位符映射表 {original_content: placeholder_id})
        """

    @abstractmethod
    def restore_sensitive(self, text: str) -> str:
        """从占位符还原原始敏感内容。
        
        Args:
            text: 包含占位符的文本
            
        Returns:
            还原后的文本
        """

    @abstractmethod
    def forget(self, user_id: str, nl_instruction: str) -> list[str]:
        """自然语言驱动的精确遗忘。
        
        Args:
            user_id: 用户ID
            nl_instruction: 自然语言遗忘指令
            
        Returns:
            解析匹配到的目标 ID 列表；不在解析阶段执行删除
        """

    @abstractmethod
    def delete_sensitive_entries(self, user_id: str, placeholder_ids: list[str]) -> list[str]:
        """删除索引中的敏感条目（软删除）。
        
        Args:
            user_id: 用户ID
            placeholder_ids: 要删除的占位符ID列表
            
        Returns:
            成功删除的占位符ID列表
        """

    @abstractmethod
    def get_sensitive_stats(self, user_id: str) -> dict:
        """获取敏感信息统计信息。
        
        Args:
            user_id: 用户ID
            
        Returns:
            统计信息字典
        """

    @abstractmethod
    def save_index(self) -> None:
        """持久化索引到文件。"""

    @abstractmethod
    def load_index(self, filepath: str) -> None:
        """从文件加载索引。
        
        Args:
            filepath: 文件路径
        """


class IngestionAdapter(ABC):
    """Owner: 3fba60e1 | Ref: Zep ingestion, Cognee ingest patterns."""

    @abstractmethod
    def ingest(self, raw: dict[str, Any], source: str) -> RawRecord:
        """Normalize raw input into RawRecord."""

    @abstractmethod
    def validate(self, record: RawRecord) -> bool:
        """Quality check before downstream processing."""


class PreferenceStore(ABC):
    """Owner: 26a9835a | Ref: Mem0 fact extract, MemoryOS long_term.

    Path B: extract() → MemoryEntry; Orchestrator calls create_memory / store_memory_entry.
    Preference module maintains local version index only (no Milvus write).
    """

    @abstractmethod
    def extract(self, records: list[RawRecord]) -> list["MemoryEntry"]:
        """Extract preferences → MemoryEntry candidates (no Milvus write)."""

    @abstractmethod
    def save(self, item: MemoryItem) -> MemoryItem:
        """Update local version index from MemoryItem (Orchestrator-compat)."""

    @abstractmethod
    def get_version_history(self, user_id: str, key: str) -> list[MemoryItem]:
        """Local version chain for conflict checks / demo."""


class KnowledgeStore(ABC):
    """Owner: cb0aeabe | Ref: Cognee KG, mem0 hybrid retrieval."""

    @abstractmethod
    def store(self, item: MemoryItem) -> MemoryItem:
        """Structured knowledge storage (workflow, cases, templates)."""

    @abstractmethod
    def retrieve(self, query: RetrievalQuery) -> RetrievalResult:
        """RAG retrieval; latency must be <= 500ms."""

    @abstractmethod
    def resolve_conflict(self, new_item: MemoryItem, existing: MemoryItem) -> MemoryItem:
        """Merge/replace/keep; target correctness >= 88%."""


class SecurityService(ABC):
    """Owner: 2abac5a6 | Ref: Cognee forget, Supermemory contradiction handling."""

    @abstractmethod
    def filter_sensitive(self, text: str) -> tuple[str, list[str], dict[str, str]]:
        """Detect and mask PII; return (filtered_text, detected_types, placeholder_map).
        
        placeholder_map: {original_content: placeholder_id}
        """

    @abstractmethod
    def restore_sensitive(self, text: str) -> str:
        """Restore original sensitive content from placeholders using the index file.
        
        Only works if the index file is accessible and contains the mapping.
        """

    @abstractmethod
    def forget(self, user_id: str, nl_instruction: str) -> list[str]:
        """Resolve natural-language forgetting targets without deleting them."""

    @abstractmethod
    def parse_forget_targets(
        self,
        instruction: str,
        candidates: list[MemoryItem],
    ) -> "ForgetResolution":
        """Resolve NL forgetting onto a general memory snapshot (P0 fix).

        Unlike ``forget`` (which only scans the sensitive index), this maps an
        instruction like「忘掉下载路径偏好」onto ordinary memory/preference
        candidates and returns hit targets with confidence + scope + safety.
        Does not delete anything.
        """

    @abstractmethod
    def delete_sensitive_entries(self, user_id: str, placeholder_ids: list[str]) -> list[str]:
        """Delete specific sensitive entries from the index (soft delete).
        Returns list of deleted placeholder_ids.
        """

    @abstractmethod
    def get_sensitive_stats(self, user_id: str) -> dict:
        """Get statistics about sensitive entries for a user.
        Returns {total_count, type_breakdown, is_deleted_count}.
        """

    @abstractmethod
    def save_index(self, filepath: str) -> None:
        """Persist the sensitive index to a file."""

    @abstractmethod
    def load_index(self, filepath: str) -> None:
        """Load the sensitive index from a file."""


class EmbeddingProvider(ABC):
    """Owner: TBD (efca9634协调) | Target: Kylin embedding SDK."""

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Batch embedding via Kylin SDK or dev fallback."""


class StorageBackend(ABC):
    """Owner: TBD (efca9634协调) | Lightweight edge storage."""

    @abstractmethod
    def save(self, item: MemoryItem) -> None: ...

    @abstractmethod
    def get(self, memory_id: str) -> MemoryItem | None: ...

    @abstractmethod
    def delete(self, memory_id: str) -> bool: ...


class MemoryLayerManager(ABC):
    """Owner: efca9634 | Ref: MemoryOS short/mid/long + LangMem background."""

    @abstractmethod
    def write_short(self, item: MemoryItem): ...

    @abstractmethod
    def promote_to_mid(self, user_id: str) -> list[MemoryItem]:
        """Consolidate short -> mid when heat threshold met."""

    @abstractmethod
    def promote_to_long(self, user_id: str) -> list[MemoryItem]:
        """Consolidate mid -> long (preference + knowledge)."""

    @abstractmethod
    def read(self, layer: MemoryLayer, user_id: str, limit: int = 10) -> list[MemoryItem]: ...
