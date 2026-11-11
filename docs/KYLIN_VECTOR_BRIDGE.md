# 系统向量数据库 SDK 桥接

两个SDK分别通过后，继续执行[真实Embedding与系统向量库联合验收](KYLIN_JOINT_EVAL.md)，计算标注集Recall@K与查询编码加检索的完整延迟。

本轮先快进同步远程 master 至 `e9927d2`，再实现桥接。参考 SDK 为本机 `D:/揭榜挂帅/VECTOR`，提交 `58ab519`（1.2.0.0-ok0.3）；未修改或复制主办方 SDK 源码。

## 调用链与范围

`MemoryOrchestrator → KylinVectorClient → ctypes → libmokoa_kylin_vector_bridge.so → VectorDB::Database → 系统向量引擎`。

所有实际数据库操作调用 SDK 的 HasCollection/CreateCollection/LoadCollection/Insert/Query/Search/Upsert/Delete；不存在降级为内存或普通 pymilvus 的路径。缺少动态库、连接失败、SDK 报错均向上抛异常。Python 仍依赖项目已有的 pymilvus 包来复用 DataType 枚举，但 kylin 后端不会创建或调用 pymilvus 原生客户端。

现有字段映射、过滤表达式、完整行更新和软/硬删除复用 `MilvusClient`。新增的是该封装下方的传输适配。原 memory/milvus 默认路线保持可用，默认配置未强行切换。本次不包含知识冲突、安全入口和用户身份边界的业务重构。

## 麒麟 V11 构建

需要系统向量引擎服务，以及与系统配套的 `libkysdk-vector-engine-client-dev`、C++17 编译器、CMake 和 nlohmann-json 开发文件。优先链接已安装的系统 SDK，不必编译整个 VECTOR 仓库；若确需编译 SDK 本身，其 CMake 还依赖 gRPC、Protobuf 与 KylinAiProto。

```bash
cmake -S native/kylin_vector_bridge -B build/kylin-vector -DCMAKE_BUILD_TYPE=Release
cmake --build build/kylin-vector --parallel
```

非标准安装位置可给 CMake 传入 `-DKYLIN_VECTOR_INCLUDE_DIR=...`（包含 Database.h 的目录）和 `-DKYLIN_VECTOR_LIBRARY=...`（系统 SDK .so）。头文件与库必须来自同一安装版本。

```bash
export OS_AGENT_MEMORY_BACKEND=kylin
export OS_AGENT_KYLIN_VECTOR_LIBRARY="$PWD/build/kylin-vector/libmokoa_kylin_vector_bridge.so"
export OS_AGENT_MILVUS_COLLECTION=mokoa_kylin_sdk
export OS_AGENT_EMBEDDING_BACKEND=kylin
export OS_AGENT_KYLIN_STRICT=1
# 填入麒麟 Embedding 的实际输出维度；不得混用 BGE 旧集合
export MEMORY_VECTOR_DIM=768
python -m uvicorn api.server:app --host 127.0.0.1 --port 18085
```

768 是配置示例，必须先核实实际输出。API 与系统向量服务应在同一用户上下文运行；SDK 默认端点为 `unix:/tmp/kylin-ai-vector-engine-<有效UID>.sock`。桥接可用 `OS_AGENT_KYLIN_VECTOR_ENDPOINT` 指定完整端点，未设置时完全使用 SDK 默认值；该配置不是 `.db` 文件路径。初始化连接和 Query/Search 的超时由 `OS_AGENT_KYLIN_VECTOR_TIMEOUT_MS` 设置，默认 5000 ms；SDK 未给其他 CRUD 提供本封装可用的逐请求超时参数。

## C ABI 与资源管理

`bridge.h` 导出 ABI 版本、创建上下文、JSON 请求、释放响应和销毁上下文五个函数。UTF-8 JSON 不暴露 C++ 类布局；动态库内部捕获异常并返回 `{ok, result}` 或 `{ok, code, error}`。每个响应由动态库 malloc，必须通过其 free 函数释放。

Python 使用 `c_void_p` 接收响应，读取后在 finally 中释放，避免 c_char_p 自动转换导致丢失原指针。每个连接串行处理请求；关闭与调用互斥，初始化失败也释放句柄。`NativeVectorBridge` 支持上下文管理；`KylinVectorClient.close()` 用于显式关闭。

## 验证与已知边界

本机 Windows 完成：

- 使用 g++ C++17 对 bridge.cpp 与克隆 SDK 的真实头文件进行语法检查，通过。临时使用 nlohmann/json v3.11.3 单头文件，不纳入项目源码。
- 实际编译加载小型 ABI 测试动态库，验证 UTF-8、返回内存释放、连接失败回收、错误 JSON、并发调用串行化、重复关闭。
- 存储契约测试覆盖已有集合字段注册、更新保留向量与版本、软/硬删除、查询分数和维度校验。

回归记录：相关测试最初37项通过。全量回归317项通过、2项失败；在未修改的远程基线 `e9927d2` 独立副本中，两项失败均复现：`test_consolidate_promotes_and_stores_preference` 期望长期条数1但得到2；`test_consolidate_mid_only_does_not_store_to_milvus` 期望0但得到1。它们涉及新自动偏好入库与晋升预期，未在本轮改写业务或放宽断言。

这些验证**不等同于链接系统 SDK 成功或麒麟服务实跑**。Windows 缺少系统 Linux .so 和向量引擎服务，最终二进制链接、字符串主键/JSON/过滤支持、强一致性及性能须在 V11 验证。

必须关注：

1. SDK Query 未暴露 limit/offset。当前只在返回后截取，不能视为分页；大型画像、统计和批量遗忘必须核实服务端结果上限。原有批量删除1000条上限仍保留，不宣称全量删除已解决。
2. 已有集合不会自动迁移或重建，SDK 公共 Database 接口未暴露描述 schema。使用独立集合，先跑通全字段写读和实际维度；不自动删除旧库。
3. SDK 示例把所有 SERVER_FAILED 当作集合不存在。本封装保留错误而不吞掉；若系统对不存在集合返回错误，需要记录精确状态再适配，不应隐藏权限或连接故障。
4. SDK 头文件说 Delete 只支持主键，示例却使用普通字段。本项目硬删除只发送 memory_id 条件，批量删除沿用先查询ID再逐条删除。
5. Query/Search 设置强一致性时间戳，目标是更新和遗忘后及时可见；其实际效果必须实测。
6. SDK 切换不会自动修复原来的跨用户 filters 覆盖、直接入库脱敏、知识冲突等业务问题。

## 两次独立进程持久化检查

设置上述动态库环境后：

```bash
python scripts/smoke_kylin_vector.py write --manifest data/kylin-vector-smoke.json
# 第一条退出后，再启动第二个 Python 进程；也可在两者之间重启系统向量服务
python scripts/smoke_kylin_vector.py verify --manifest data/kylin-vector-smoke.json
```

此脚本新建唯一测试集合，写入并更新合成记录；第二进程核对持久化、查询、用户过滤和物理删除，仅清除该测试ID，不删除集合。使用合成向量以单独验证数据库，不能计为系统 Embedding 验收。之后还须通过 API 联合真实麒麟 Embedding 重跑语义召回率、延迟和完整遗忘链路。
