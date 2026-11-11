"""End-to-end recall with deterministic vectors that deliberately miss documents.

Exercises query_memory, vector search, lexical pool, aliases and reranking.
This is not a Kylin deployment or embedding quality benchmark.
"""
import pytest

from src.common.models import RetrievalQuery, MemoryType
from src.core.orchestrator import MemoryOrchestrator
from src.core.recall_ranker import extract_project_tags, knowledge_intent, open_domain_semantic, preference_intent, term_overlap
from src.knowledge.memory_service.embedding_client import EmbeddingClient
from src.knowledge.memory_service.in_memory_client import InMemoryMilvusClient


@pytest.fixture
def recall_store(monkeypatch):
    monkeypatch.setattr(EmbeddingClient, "embed", lambda self, text: [1.0, 0.0])

    class RecordingStore(InMemoryMilvusClient):
        def __init__(self):
            super().__init__()
            self.calls = []

        def search(self, embedding, filters, top_k=5):
            hits = super().search(embedding, filters, top_k)
            self.calls.append((embedding, dict(filters), [h["memory_id"] for h in hits]))
            return hits

    store = RecordingStore()

    def add(key, text, *, aliases=(), kind="document_chunk", user="u", latest=True, scene="coding"):
        store.insert(key, text, [0.0, 1.0], {
            "user_id": user, "scene_id": scene, "memory_type": kind,
            "is_latest": latest, "project_aliases": list(aliases), "file_kind": "txt",
        })

    # More than fetch_k nearest neighbors, none of the answer documents in top 20.
    for i in range(25):
        store.insert(f"noise-{i}", "星河笔记配置路径偏好 /opt/xinghe/notes/config.yaml", [1.0, 0.0], {
            "user_id": "u", "scene_id": "coding", "memory_type": "preference",
            "is_latest": True, "project_aliases": ["星河笔记", "XingHeNotes"],
        })
    add("bridge", "星河笔记配置变更通知 / XingHe Notes Config Update\nFACT project=XingHeNotes port=9090")
    add("manual", "XingHe Notes Project Manual\nProject: XingHe Notes / XingHeBiji\nOwner: Zhang San\nDefault HTTP port: 9090\nHealth: /health")
    add("course", "Project: CourseAssistant\nport: 8080\nHealth: /ready", aliases=("课程助手", "CourseAssistant"))
    add("office", "Printer share name: KYLIN-PRINT-01. Meeting room WiFi SSID: Kylin-Office-Guest")
    add("pip", "pip install 使用 PyPI 镜像可以加速依赖安装。")
    add("old", "Project: XingHe Notes\nDefault HTTP port: 8080", latest=False)
    add("foreign", "星河笔记健康检查负责人打印机端口", user="other")
    add("other-scene", "星河笔记健康检查负责人打印机端口", scene="private")
    return MemoryOrchestrator(milvus=store), store


@pytest.mark.parametrize("query,expected", [
    ("装包很慢有什么办法？", "pip"),
    ("打印机共享名是什么", "office"),
    ("星河笔记手册端口", "manual"),
    ("星河笔记健康检查地址", "manual"),
    ("星河笔记谁负责", "manual"),
    ("手册里的默认端口是多少", "manual"),
    ("课程助手端口是多少", "course"),
])
def test_recall_recovers_documents_outside_vector_pool(recall_store, query, expected):
    orchestrator, store = recall_store
    result = orchestrator.recall(RetrievalQuery(query=query, user_id="u", scene_id="coding", top_k=5))
    assert expected not in store.calls[0][2]
    assert result.items and result.items[0].id == expected
    assert any(vector is None for vector, _, _ in store.calls)
    assert all(f["user_id"] == "u" and f["is_latest"] is True and f["scene_id"] == "coding"
               for _, f, _ in store.calls)
    assert not {"old", "foreign", "other-scene"} & {i.id for i in result.items}
    assert result.latency_ms >= 0


@pytest.mark.parametrize("query", ["明天股价多少？", "星河笔记股价是多少", "如何预测明天股价？"])
def test_no_evidence_stays_empty_even_with_project_and_english_docs(recall_store, query):
    orchestrator, _ = recall_store
    assert not orchestrator.recall(RetrievalQuery(query=query, user_id="u", top_k=5)).items


def test_default_port_is_not_preference():
    assert knowledge_intent("手册里的默认端口是多少")
    assert not preference_intent("手册里的默认端口是多少")
    assert preference_intent("我的默认下载路径")


@pytest.mark.parametrize("mode", ["auto", "hybrid"])
def test_english_project_keeps_lexical_recovery(recall_store, mode):
    orchestrator, store = recall_store
    result = orchestrator.recall(RetrievalQuery(
        query="XingHeNotes default port", user_id="u", scene_id="coding",
        retrieval_mode=mode, top_k=5))
    assert result.items
    assert any(vector is None for vector, _, _ in store.calls)
    assert result.strategy == "query_memory+lexical"


def test_explicit_hybrid_can_recover_english_office_document(recall_store):
    orchestrator, store = recall_store
    result = orchestrator.recall(RetrievalQuery(
        query="printer", user_id="u", retrieval_mode="hybrid", top_k=5))
    assert "office" in {item.id for item in result.items}
    assert any(vector is None for vector, _, _ in store.calls)


def test_open_domain_semantic_routes_english_general_questions():
    # 纯英文通用问句 → 走语义、跳过办公词面补召（修复 #3，P95 破 500ms 主因）。
    assert open_domain_semantic("how to configure docker network settings", [])
    assert open_domain_semantic("What is the default HTTP port?", [])
    # 含中文 → 仍走词面补召（中文语境）。
    assert not open_domain_semantic("星河笔记手册端口是多少", [])
    # 命中已知项目别名的英文 / 含英文办公短语 → 保留词面补召。
    assert not open_domain_semantic("printer share name", [])
    assert not open_domain_semantic("project manual port", [])


def test_english_open_domain_skips_lexical_fallback(recall_store):
    orchestrator, store = recall_store
    result = orchestrator.recall(RetrievalQuery(
        query="how to configure docker network settings", user_id="u", top_k=5))
    # 修复 #3：英文通用问句在 auto 下由 open_domain_semantic 判定走语义，
    # 不触发全表词面扫描；策略为 query_memory:auto-semantic（等价显式 semantic）。
    assert result.strategy == "query_memory:auto-semantic"
    assert all(vector is not None for vector, _, _ in store.calls)


def test_topic_synonyms_are_symmetric_and_not_substring_matches():
    assert term_overlap("健康检查地址", "Health: /health") > 0
    assert term_overlap("printer", "打印机共享名") > 0
    assert term_overlap("port", "support export report") == 0


def test_project_scope_does_not_match_incidental_other_project_mention(recall_store):
    orchestrator, store = recall_store
    store._records["manual"]["content"] += "\nCourseAssistant is a DIFFERENT project."
    result = orchestrator.recall(RetrievalQuery(query="课程助手端口", user_id="u", top_k=5))
    assert [i.id for i in result.items] == ["course"]


def test_explicit_knowledge_scope_also_recovers_english_document(recall_store):
    orchestrator, _ = recall_store
    result = orchestrator.recall(RetrievalQuery(
        query="星河笔记手册端口", user_id="u", scene_id="coding",
        memory_type=MemoryType.KNOWLEDGE, top_k=1))
    assert [i.id for i in result.items] == ["manual"]


def test_caller_cannot_override_user_and_old_port_requires_history(recall_store):
    orchestrator, _ = recall_store
    query = RetrievalQuery(query="星河笔记端口", user_id="u", scene_id="coding", top_k=10)
    current = orchestrator.recall(query, filters={"user_id": "other"})
    assert "old" not in {i.id for i in current.items}
    assert all(i.user_id == "u" and i.id != "course" for i in current.items)
    history = orchestrator.recall(query, filters={"is_latest": False})
    assert "old" in {i.id for i in history.items}


def test_alias_resolution_is_corpus_driven_not_project_specific(recall_store):
    orchestrator, store = recall_store
    for record in store._records.values():
        for source, target in (("星河笔记", "青山档案"), ("XingHeNotes", "GreenArchive"),
                               ("XingHe Notes", "Green Archive"), ("XingHeBiji", "ArchiveService")):
            record["content"] = record["content"].replace(source, target)
            record["project_aliases"] = [a.replace(source, target) for a in record.get("project_aliases", [])]
    result = orchestrator.recall(RetrievalQuery(query="青山档案健康检查", user_id="u", scene_id="coding", top_k=1))
    assert [i.id for i in result.items] == ["manual"]


def test_ingest_prefix_is_not_a_project_alias():
    tags = extract_project_tags(
        "用户上传PDF「01_xinghe_notes_manual.pdf」：XingHe Notes Project Manual\n"
        "Project: XingHe Notes / XingHeBiji\nDefault HTTP port: 8080"
    )
    assert "用户上传" not in tags["project_aliases"]
    assert "PDF" not in {alias.upper() for alias in tags["project_aliases"]}
    assert "XingHe Notes" in tags["project_aliases"]
    assert ("星河笔记", "XingHe Notes") in extract_project_tags(
        "星河笔记配置变更通知 / XingHe Notes Config Update\nFACT project=XingHeNotes port=9090"
    )["bilingual_pairs"]


def test_upload_wrapper_and_dual_project_notice_do_not_merge_scopes(recall_store):
    orchestrator, store = recall_store
    meta = {
        "user_id": "u", "scene_id": "coding", "memory_type": "document_chunk",
        "is_latest": True, "file_kind": "pdf",
    }
    store.insert(
        "upload-manual",
        "用户上传PDF「01.pdf」：XingHe Notes Project Manual\nProject: XingHe Notes\nDefault HTTP port: 8080",
        [0.0, 1.0],
        meta,
    )
    store.insert(
        "upload-office",
        "用户上传PDF「02.pdf」：Printer share name: KYLIN-PRINT-01",
        [0.0, 1.0],
        meta,
    )
    store.insert(
        "upload-update",
        "用户上传Word「04.docx」：星河笔记配置变更通知 / XingHe Notes Config Update\n"
        "FACT project=XingHeNotes port=9090\nFACT project=CourseAssistant port=8080\n"
        "旧端口 8080 仅保留给课程助手 CourseAssistant",
        [0.0, 1.0],
        meta,
    )
    course = orchestrator.recall(RetrievalQuery(query="课程助手端口", user_id="u", scene_id="coding", top_k=5))
    course_ids = {item.id for item in course.items}
    assert "course" in course_ids or "upload-update" in course_ids
    assert not course_ids & {"manual", "upload-manual", "upload-office"}
    printer = orchestrator.recall(RetrievalQuery(query="打印机共享名是什么", user_id="u", scene_id="coding", top_k=5))
    assert {item.id for item in printer.items} <= {"office", "upload-office"}
