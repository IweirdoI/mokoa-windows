"""短期记忆并发安全回归测试。

FastAPI 线程池并发 ingest 同一 user_id 时，若 _buckets/_items_by_id 多步改写无锁，
会覆盖或丢失写入、甚至破坏列表结构。加 RLock 后应保证并发写入不丢写、不抛异常。
"""

import threading

from src.common.models import MemoryItem, MemoryLayer, MemoryType
from src.core.short_term import ShortTermMemory


def _make(i: int) -> MemoryItem:
    return MemoryItem(
        id=f"m{i}",
        memory_type=MemoryType.KNOWLEDGE,
        layer=MemoryLayer.SHORT,
        content=f"c{i}",
        user_id="u1",
        metadata={"fingerprint": f"fp-{i}"},
    )


def test_concurrent_writes_no_crash_and_no_loss():
    st = ShortTermMemory(capacity=1000)
    n = 200

    def worker(i: int):
        st.write(_make(i))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    items = st.read("u1", limit=10000)
    assert len(items) == n, f"expected {n} got {len(items)}"
    assert st.get("m0") is not None
    assert st.stats("u1").count == n
