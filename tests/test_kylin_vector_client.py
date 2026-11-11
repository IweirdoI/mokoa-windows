"""Bridge ownership/error tests and reuse of the existing memory contract.

The tiny compiled fixture exercises ctypes ABI, not a real system vector SDK.
"""
import ctypes
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.knowledge.memory_service import kylin_vector_client as module


@pytest.fixture
def abi_library(tmp_path):
    compiler = shutil.which("gcc") or shutil.which("cc")
    if not compiler:
        pytest.skip("C compiler needed for actual ctypes ABI fixture")
    source = tmp_path / "fixture.c"
    source.write_text(r'''
#include <stdlib.h>
#include <string.h>
#ifdef _WIN32
#define API __declspec(dllexport)
#else
#define API
#endif
static int frees = 0, destroys = 0;
API int mokoa_vector_abi_version(void) { return 1; }
API void* mokoa_vector_create(void) { return malloc(1); }
API char* mokoa_vector_call(void* h, const char* req) {
  const char* prefix = "{\"ok\":true,\"result\":";
  char* p = (char*)malloc(strlen(req) + 128);
  if (strstr(req, "fail_sdk")) strcpy(p,"{\"ok\":false,\"code\":42,\"error\":\"fixture failure\"}");
  else if (strstr(req,"bad_json")) strcpy(p,"invalid");
  else { strcpy(p,prefix); strcat(p,req); strcat(p,"}"); }
  return p;
}
API void mokoa_vector_free(void* p) { ++frees; free(p); }
API void mokoa_vector_destroy(void* p) { ++destroys; free(p); }
API int fixture_frees(void) { return frees; }
API int fixture_destroys(void) { return destroys; }
''', encoding="utf-8")
    output = tmp_path / ("fixture.dll" if sys.platform == "win32" else "fixture.so")
    subprocess.run([compiler, "-shared", "-fPIC", str(source), "-o", str(output)], check=True, capture_output=True)
    return str(output)


def test_real_ctypes_ownership_unicode_errors_and_concurrent_calls(abi_library):
    bridge = module.NativeVectorBridge(abi_library)
    assert bridge.call("echo", content="中文与😀")['content'] == "中文与😀"
    with pytest.raises(module.KylinVectorError, match="42.*fixture failure"):
        bridge.call("fail_sdk")
    with pytest.raises(ValueError):
        bridge.call("bad_json")
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(lambda i: bridge.call("echo", value=i)['value'], range(12)))
    assert values == list(range(12))
    assert bridge._lib.fixture_frees() == 16  # connect + three calls + twelve
    bridge.close()
    bridge.close()
    assert bridge._lib.fixture_destroys() == 1
    with pytest.raises(module.KylinVectorError, match="closed"):
        bridge.call("echo")


def test_connect_failure_releases_handle(abi_library):
    lib = ctypes.CDLL(abi_library)
    with pytest.raises(module.KylinVectorError, match="fixture failure"):
        module.NativeVectorBridge(abi_library, endpoint="fail_sdk")
    assert lib.fixture_destroys() == 1
    assert lib.fixture_frees() == 1


def test_missing_library_never_falls_back(tmp_path):
    with pytest.raises(FileNotFoundError):
        module.NativeVectorBridge(str(tmp_path / "missing.so"))


class RecordingBridge:
    def __init__(self, *args, **kwargs):
        self.calls = []
        self.row = None
        self.closed = False

    def call(self, op, **payload):
        self.calls.append((op, payload))
        if op == "has_collection":
            return True  # existing collections must still register column types
        if op in ("insert", "upsert"):
            self.row = payload['rows'][0].copy()
            return True
        if op == "query":
            return [self.row.copy()] if self.row else []
        if op == "search":
            return [[dict(entity=self.row.copy(), distance=.9)]]
        return True

    def close(self):
        self.closed = True


def test_existing_schema_update_soft_delete_and_search_contract(monkeypatch):
    monkeypatch.setattr(module, 'NativeVectorBridge', RecordingBridge)
    client = module.KylinVectorClient(collection="test", vector_dim=3)
    assert client.insert("uuid-string", "中文知识", [1., 0., 0.], {'user_id':'alice','version':2})
    bridge = client.bridge
    fields = bridge.calls[-1][1]['fields']
    assert next(f for f in fields if f['name']=='memory_id')['is_primary']
    assert next(f for f in fields if f['name']=='embedding')['dim'] == 3
    assert next(f for f in fields if f['name']=='metadata')['type'] == 23
    assert client.update("uuid-string", {'is_latest':False})
    assert bridge.row['content'] == '中文知识'
    assert bridge.row['embedding'] == [1., 0., 0.]
    assert bridge.row['version'] == 2
    hits = client.search([1.,0.,0.], {'user_id':'alice','scene_id':'office'})
    assert hits[0]['score'] == .9
    assert 'user_id == "alice"' in bridge.calls[-1][1]['filter']
    assert 'is_deleted == false' in bridge.calls[-1][1]['filter']
    assert client.delete('uuid-string', soft=True)
    assert bridge.row['is_deleted'] is True
    assert client.delete('uuid-string', soft=False)
    assert bridge.calls[-1] == ('delete', {'collection':'test','filter':'memory_id == "uuid-string"'})
    client.close()
    assert bridge.closed


def test_vector_dimension_rejected_before_sdk_write(monkeypatch):
    monkeypatch.setattr(module, 'NativeVectorBridge', RecordingBridge)
    client = module.KylinVectorClient(collection="test", vector_dim=3)
    with pytest.raises(ValueError, match="dim"):
        client.insert("id", "text", [1.], {})
    assert not any(op=='insert' for op,_ in client.bridge.calls)
    client.close()


def test_auto_backend_stays_memory_under_pytest(tmp_path):
    from src.core.factory import resolve_memory_backend

    assert resolve_memory_backend({'memory_backend': 'auto'}, project_root=tmp_path) == 'memory'


def test_auto_backend_locks_kylin_on_capable_host(monkeypatch, tmp_path):
    from src.core import factory

    library = tmp_path / 'libmokoa_kylin_vector_bridge.so'
    library.write_bytes(b'x')
    monkeypatch.delenv('OS_AGENT_MEMORY_BACKEND', raising=False)
    monkeypatch.setenv('OS_AGENT_KYLIN_VECTOR_LIBRARY', str(library))
    # resolve_memory_backend 走 kylin 分支时会经 _lock_kylin_runtime 用
    # os.environ.setdefault 写入以下全局变量，未按 monkeypatch 管理会泄漏到后续测试。
    # 这里先以目标值登记，使 monkeypatch 在测试结束时把原始值（通常为未设置）还原。
    monkeypatch.setenv('OS_AGENT_EMBEDDING_BACKEND', 'kylin')
    monkeypatch.setenv('OS_AGENT_KYLIN_STRICT', '1')
    monkeypatch.setenv('OS_AGENT_MILVUS_COLLECTION', 'mokoa_kylin_sdk')
    monkeypatch.setattr(factory, 'under_pytest', lambda: False)
    monkeypatch.setattr(factory, 'host_can_load_kylin_sdk', lambda: True)
    assert factory.resolve_memory_backend({'memory_backend': 'auto'}, project_root=tmp_path) == 'kylin'
    assert os.environ['OS_AGENT_EMBEDDING_BACKEND'] == 'kylin'
    assert os.environ['OS_AGENT_KYLIN_STRICT'] == '1'


@pytest.mark.parametrize('broken', [False, True])
def test_factory_selects_sdk_and_propagates_failure(monkeypatch, tmp_path, broken):
    from src.core import factory
    library = tmp_path / 'libmokoa_kylin_vector_bridge.so'
    library.write_bytes(b'x')
    monkeypatch.setenv('OS_AGENT_KYLIN_VECTOR_LIBRARY', str(library))
    monkeypatch.setenv('OS_AGENT_MEMORY_BACKEND', 'kylin')
    # 同上：登记 _lock_kylin_runtime 会写入的全局变量，确保测试结束还原，不污染后续用例。
    monkeypatch.setenv('OS_AGENT_EMBEDDING_BACKEND', 'kylin')
    monkeypatch.setenv('OS_AGENT_KYLIN_STRICT', '1')
    monkeypatch.setenv('OS_AGENT_MILVUS_COLLECTION', 'mokoa_kylin_sdk')
    monkeypatch.setenv('MEMORY_VECTOR_DIM', '3')
    monkeypatch.setattr(factory, '_load_default_config', lambda: {
        'storage':{'staging_backend':'memory'},
        'preference':{'storage_dir':str(tmp_path/'preferences'),'use_llm':False},
        'index_path':str(tmp_path/'security.json'),
    })
    def unavailable(*args, **kwargs):
        raise module.KylinVectorError('SDK unavailable')
    monkeypatch.setattr(module, 'NativeVectorBridge', unavailable if broken else RecordingBridge)
    if broken:
        with pytest.raises(module.KylinVectorError, match='SDK unavailable'):
            factory.create_orchestrator()
    else:
        orchestrator = factory.create_orchestrator()
        assert isinstance(orchestrator._milvus, module.KylinVectorClient)
        assert orchestrator._milvus.vector_dim == 3
        orchestrator._milvus.close()
