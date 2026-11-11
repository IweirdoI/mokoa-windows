# 麒麟 AI 运行时与向量客户端成套对齐

本文只处理 **系统包 ABI**，不改 MOKOA 代码。目标机是银河麒麟桌面 **V11 2603**（x86_64，glibc **2.38**）。Cursor/KARE（glibc 2.31）不能用来验证这些 `.so`。

MOKOA 已经按本机 **新向量客户端** 改过桥接；向量引擎烟测通过。当前挡住完整验收的是：文本 Embedding 的系统进程 `kylin-ai-runtime` 起不来，socket 建不出来。

## 1. 要处理的到底是什么

系统里有 **两条独立调用链**，共用一部分 `.so`。

```text
文本 Embedding（现在挂了）
  Python
    → libkysdk-coreai-embedding.so.1
      → unix:path=/tmp/.kylin-ai-runtime-unix/<uid>/core-textembedding.sock
        → /usr/bin/kylin-ai-runtime          ← 这个二进制起不来

向量库（已经通了，不要弄坏）
  Python
    → build/kylin-vector/libmokoa_kylin_vector_bridge.so
      → libkysdk-vector-engine-client.so.1   ← 本机已是新 ABI
        → unix:/tmp/kylin-ai-vector-engine-<uid>.sock
          → kylin-ai-vector-engine
```

`kylin-ai-runtime` **动态链接**了 `libkysdk-vector-engine-client`。它启动时就要解析 `VectorDB::ConnectParam` 的构造函数。本机客户端已经删掉无参构造，runtime 还在找旧符号，动态链接器直接退出 127：

```text
/usr/bin/kylin-ai-runtime: symbol lookup error:
undefined symbol: _ZN8VectorDB12ConnectParamC1Ev
```

`c++filt` 解码后是：

```text
VectorDB::ConnectParam::ConnectParam()
```

也就是 **无参数构造函数**。这不是缺模型、不是 socket 权限、也不是 MOKOA 脚本写错。是两个已安装的麒麟包不是同一代 SDK。

2026-09-06 联合验收能跑，说明当时 runtime 进程已经在、socket 存在。2026-09-07 再裸跑 `/usr/bin/kylin-ai-runtime` 才暴露这个链接错误。可能原因：登录会话里曾经由 AI 助手带起过 runtime（或当时客户端还是旧 ABI）；现在进程没了，直接启动系统二进制就会撞上新客户端。

## 2. 本机已确认的版本（2026-09-07 15:46 宿主终端）

`dpkg` / `ldd` / `nm -D` 已对上，缺口就是 runtime 与向量客户端不是一代。

| 包 / 文件 | 确认版本 | 角色 | 状态 |
|---|---|---|---|
| `kylin-ai-runtime` | `1.1.0.1-0k1.32` | Embedding 运行时进程 | `ldd` 链到下面这个 `.so`；启动要无参 `ConnectParam()` |
| `libkysdk-vector-engine-client` | `1.2.0.0-0k1.1` | 向量客户端 `.so` | **新 ABI**：只有 `ConnectParam(appId)` 和三参 TCP 构造，**没有** `C1Ev` |
| `libkysdk-vector-engine-client-dev` | `1.2.0.0-0k1.1` | 头文件 | 与运行库同代 |
| `libkylin-coreai-embedding` | `1.2.0.0-0k0.4` | Embedding C SDK | 库在；缺的是 runtime socket |
| `libkylin-coreai-embedding-dev` | `1.2.0.0-0k0.4` | 开发文件 | 已装 |
| `kylin-ai-vector-engine` | 此前 `1.2.0.1-0k0.11` | 向量引擎守护进程 | 不在这次 `dpkg -l` 过滤里；勿回退 |
| 设置里「麒麟AI子系统」 | `1.2.0.0-0k0.3` | 安装目录卡片 | 不是启动按钮 |

客户端已导出的 `ConnectParam`（`nm -D` 原文）：

```text
T ConnectParam::SetConnectTimeout(unsigned int)
T ConnectParam::ConnectParam(std::string)                          # C1 / C2
T ConnectParam::ConnectParam(std::string, std::string const&, unsigned short)
T ConnectParam::ConnectTimeout() const
T ConnectParam::Uri() const
T ConnectParam::AppId() const
```

没有 `_ZN8VectorDB12ConnectParamC1Ev`。MOKOA 桥接用的就是 `ConnectParam("mokoa")` 和 `Uri()`，和这张表一致。

MOKOA `native/kylin_vector_bridge/bridge.cpp` 按新客户端适配（当时头文件是 0k0.7，现运行库已是 0k1.1，构造函数集合相同）：

- `ConnectParam("mokoa")`，没有 `ConnectParam()`
- 用 `Uri()`，没有 `Host()` / `SetUri()`
- `IndexDesc` 四个参数

所以：**不要回退向量客户端来迁就旧 runtime**，否则已经通过的向量烟测和已编译的 `libmokoa_kylin_vector_bridge.so` 会一起坏。

## 3. 「成套」指哪一组包

要对齐的是 **同一代麒麟 AI SDK**，不是单独换一个 `.so`。按功能分成两套，必须代际一致。

### 3.1 Embedding 套（当前缺口）

至少成对：

- `kylin-ai-runtime`（提供 `/usr/bin/kylin-ai-runtime` 和 embedding socket）
- 它所链接的 `libkysdk-vector-engine-client`（同一 SONAME：`libkysdk-vector-engine-client.so.1`）
- `libkylin-coreai-embedding` / `libkysdk-coreai-embedding`（Python ctypes 加载的 C API）
- 对应 `-dev` 包（只在需要重编或核对头文件时）

runtime 往往还依赖同代的 `kylin-ai-engine`、助手或子系统元包。拉包时按 `dpkg -I` / `Depends:` 把依赖一起收下，不要只拷贝一个二进制。

### 3.2 向量套（已经对齐，作为约束）

不要为了救 runtime 把下面这套拆开：

- `kylin-ai-vector-engine`（此前 `1.2.0.1-0k0.11`）
- `libkysdk-vector-engine-client` `1.2.0.0-0k1.1`
- MOKOA 已编译的 `libmokoa_kylin_vector_bridge.so`

判定「同一代」的硬条件（必须同时满足）：

1. **runtime 需要的 `ConnectParam` 符号，在客户端 `.so` 里都存在**（`nm -D` 对得上）。
2. **MOKOA 桥接需要的新符号仍然存在**：至少能构造 `ConnectParam(std::string)`，并且没有把 `IndexDesc` 变回旧的五参数布局。
3. **向量引擎守护进程仍能被这个客户端连上**（socket 协议没换成另一代）。
4. 架构都是 `amd64`，glibc 不低于 2.38，来源是 **V11 2603 仓库**，不是随便一个 openKylin / 其它 SP 的包。

## 4. 推荐处理顺序

只选一条路，做完用第 6 节验收。优先从上往下。

### 方案 A（首选）：升级 `kylin-ai-runtime` 到 1.2.x 同一代

保持现有向量客户端 **1.2.0.0-0k1.1** 不动，去找一个 **按新 ABI 编译** 的 runtime。

新 runtime 的 `NEEDED` 里仍然是 `libkysdk-vector-engine-client.so.1`，但二进制里 **不再引用** `_ZN8VectorDB12ConnectParamC1Ev`，而是引用带 `appId` 的构造（mangling 会含 `NSt7__cxx1112basic_string` 或等价形式）。

适合「把系统库下拉到本地、做成适合咱们这台 V11 的离线包」：下载的是 **新 runtime + 现有 client 能对上的那一套**，不是旧 SDK 整包。

**本机仓库已有候选（2026-09-07 15:49）：**

| 项 | 值 |
|---|---|
| 已安装 | `kylin-ai-runtime` `1.1.0.1-0k1.32`（不在 2603 当前候选里，pin 100） |
| 应升级到 | `kylin-ai-runtime` **`1.2.0.4-0k0.1`** |
| 来源 | `http://archive.kylinos.cn/kylin/KYLIN-ALL` **`11.0-kylin-2603/main`** |
| `.deb` | `pool/main/k/kylin-ai-runtime/kylin-ai-runtime_1.2.0.4-0k0.1_amd64.deb` |
| SHA256 | `7126f18bf4e1af043eaea378591cecb93dc78897cd8d58163cc0f53eeb0f22d8` |
| 对本机 client 的 Depends | `libkysdk-vector-engine-client (>= 1.2.0.0)`（本机已是 `1.2.0.0-0k1.1`，满足） |
| glibc | `libc6 (>= 2.38)`，与宿主一致 |
| 不要装 | `1.0.0.0-ok0.16`（`11.0-kylin/universe`，更旧） |

已装的 1.1 二进制 `nm -D` 只有一行：`U _ZN8VectorDB12ConnectParamC1Ev`。2603 仓库的 1.2.0.4 才是方案 A 的目标包。新包比旧包多依赖 OpenCV 和 `libpython3.12t64`，安装前用模拟看会不会连带别的包。

### 方案 B（隔离试验，不要覆盖 `/usr/lib`）：给旧 runtime 单独准备旧客户端

仅用于确认「旧 runtime + 旧 client 能否建 embedding socket」。用私有目录：

```text
~/mokoa-kylin-pkgs/runtime-old/usr/lib/x86_64-linux-gnu/libkysdk-vector-engine-client.so.1
```

启动时：

```bash
LD_LIBRARY_PATH="$HOME/mokoa-kylin-pkgs/runtime-old/usr/lib/x86_64-linux-gnu" \
  /usr/bin/kylin-ai-runtime
```

系统默认的 `/usr/lib/.../libkysdk-vector-engine-client.so.1` 仍给 MOKOA 和向量引擎用。

风险：旧客户端若连不上正在跑的 **新** `kylin-ai-vector-engine`，runtime 可能起来了但内部报错。这条只证明符号问题，不作为长期部署。

### 方案 C（不推荐）：整套回退向量客户端到旧 ABI

把 `libkysdk-vector-engine-client` 退回带 `ConnectParam()` 的版本，通常还要回退 `kylin-ai-vector-engine`，并 **改回** MOKOA `bridge.cpp` 再重编。已经通过的向量烟测作废。只有方案 A/B 都找不到可用 runtime 时才考虑。

### 不要做的事

- 不要改 `bridge.cpp` 去重新调用 `ConnectParam()` 来「兼容」runtime。桥接加载的是客户端库，修不了 `/usr/bin/kylin-ai-runtime` 自身的 undefined symbol。
- 不要把随机 `.so` 覆盖进 `/usr/lib/x86_64-linux-gnu/`。
- 不要用 BGE / 哈希向量顶替后声称麒麟 Embedding 通过。
- 不要在 KARE/Cursor 里安装或验证这些包。
- 不要拿 Gitee `openkylin/libkysdk-vector-engine-client` 源码树直接替换系统 `.so`；那是参考 SDK，和 V11 2603 的引擎协议不一定一致。
- 不要只升级/回退其中一个包。

## 5. 拉包前：在麒麟 V11 宿主导出本机清单

在 **系统终端**（不是 Cursor）执行，把输出保存到 `data/reports/kylin-pkg-inventory.txt`。后续选包以这份为准。

```bash
mkdir -p ~/projects/MOKOA/data/reports
{
  echo "=== host ==="
  hostname; id; lsb_release -a 2>/dev/null
  /lib/x86_64-linux-gnu/libc.so.6 | head -2

  echo "=== installed ==="
  dpkg -l | grep -Ei 'kylin-ai-runtime|kysdk-vector-engine|kylin-ai-vector-engine|kylin-coreai-embedding|kysdk-coreai-embedding|kylin-ai-engine|kylin-aiassistant'

  echo "=== policy ==="
  apt-cache policy \
    kylin-ai-runtime \
    libkysdk-vector-engine-client \
    libkysdk-vector-engine-client-dev \
    kylin-ai-vector-engine \
    libkylin-coreai-embedding \
    libkylin-coreai-embedding-dev

  echo "=== runtime linkage ==="
  ls -l /usr/bin/kylin-ai-runtime
  dpkg -S /usr/bin/kylin-ai-runtime
  ldd /usr/bin/kylin-ai-runtime
  nm -D /usr/bin/kylin-ai-runtime | grep ConnectParam || true
  # 期待看到：U _ZN8VectorDB12ConnectParamC1Ev

  echo "=== client symbols ==="
  ls -l /usr/lib/x86_64-linux-gnu/libkysdk-vector-engine-client.so*
  nm -D /usr/lib/x86_64-linux-gnu/libkysdk-vector-engine-client.so.1 | grep ConnectParam
  # 新 ABI 应有带 appId 的构造，没有 C1Ev 无参构造

  echo "=== sockets ==="
  ls -l /tmp/kylin-ai-vector-engine-*.sock
  ls -l /tmp/.kylin-ai-runtime-unix/1000/core-textembedding.sock 2>/dev/null || echo "embedding socket missing"

  echo "=== headers (if dev installed) ==="
  sed -n '1,80p' /usr/include/kysdk-vector-engine-client/types/ConnectParam.h 2>/dev/null || echo "no ConnectParam.h"
} | tee ~/projects/MOKOA/data/reports/kylin-pkg-inventory.txt
```

候选包是否可用，用符号表判定，不要只看版本号字符串：

```bash
# 某个解包后的 runtime 是否还在要旧符号？
nm -D path/to/kylin-ai-runtime | grep '_ZN8VectorDB12ConnectParamC1Ev' && echo "OLD runtime, needs default ctor"

# 某个解包后的 client 有没有无参构造？
nm -D path/to/libkysdk-vector-engine-client.so.1 | grep '_ZN8VectorDB12ConnectParamC1Ev' \
  && echo "OLD client" || echo "NEW client (no default ctor)"
```

新客户端上还应该能看到带 `appId` 的构造（具体 mangling 以 `nm` 为准），例如 `ConnectParam(std::string)` / `ConnectParam(std::string, std::string, unsigned short)`。

## 6. 本地拉包、做成「适合这台 V11」的离线目录

建议目录：

```text
~/mokoa-kylin-pkgs/
  inventory.txt          # 第 5 节输出
  apt-cache/             # 从本机仓库下载的 .deb
  extract-runtime-new/   # 方案 A 解包
  extract-runtime-old/   # 方案 B 解包
  NOTES.md               # 你选中的版本和 nm 对比结果
```

### 6.1 先看仓库里有哪些版本

```bash
apt-cache madison kylin-ai-runtime libkysdk-vector-engine-client kylin-ai-vector-engine
apt-cache show kylin-ai-runtime | sed -n '1,80p'
apt-cache depends kylin-ai-runtime
```

若本机仓库没有更新的 runtime，需要从 **同一套 V11 2603** 的软件源 / 安装镜像 / 麒麟软件商店缓存里取 `.deb`。来源必须可追：包名、版本、架构、SHA256。不要从不明网盘混入其它发行版的库。

商店或 apt 缓存常见位置（有就拷出来）：

```bash
ls /var/cache/apt/archives/*kylin-ai-runtime* \
   /var/cache/apt/archives/*kysdk-vector-engine-client* 2>/dev/null
```

本机仓库能下的话：

```bash
mkdir -p ~/mokoa-kylin-pkgs/apt-cache
cd ~/mokoa-kylin-pkgs/apt-cache
apt-get download kylin-ai-runtime libkysdk-vector-engine-client libkysdk-vector-engine-client-dev
# 需要依赖时按 apt-cache depends 继续 download，不要漏 SONAME 依赖
```

指定版本（本机就是这一条）：

```bash
apt-get download kylin-ai-runtime=1.2.0.4-0k0.1
```

### 6.1.1 本机升级步骤（先核符号再 apt）

不要装 `1.0.0.0-ok0.16`。先下载、解包、确认新二进制不再 `U ConnectParamC1Ev`，再安装。

```bash
mkdir -p ~/mokoa-kylin-pkgs/apt-cache ~/mokoa-kylin-pkgs/extract-runtime-new
cd ~/mokoa-kylin-pkgs/apt-cache
apt-get download kylin-ai-runtime=1.2.0.4-0k0.1
sha256sum kylin-ai-runtime_1.2.0.4-0k0.1_amd64.deb
# 应为 7126f18bf4e1af043eaea378591cecb93dc78897cd8d58163cc0f53eeb0f22d8

dpkg-deb -x kylin-ai-runtime_1.2.0.4-0k0.1_amd64.deb ~/mokoa-kylin-pkgs/extract-runtime-new
BIN=~/mokoa-kylin-pkgs/extract-runtime-new/usr/bin/kylin-ai-runtime
nm -D "$BIN" | grep ConnectParam
# 通过：没有 U _ZN8VectorDB12ConnectParamC1Ev
# 更好：出现带 basic_string 的 ConnectParamC1（appId 构造）

# 看会装哪些依赖，不应出现向量客户端降级
apt-get -s install kylin-ai-runtime=1.2.0.4-0k0.1
```

`nm` 通过且模拟安装不降级 `libkysdk-vector-engine-client` 之后：

```bash
sudo apt-get install kylin-ai-runtime=1.2.0.4-0k0.1
dpkg -l kylin-ai-runtime libkysdk-vector-engine-client | awk '{print $1,$2,$3}'
# runtime 应为 1.2.0.4-0k0.1；client 仍为 1.2.0.0-0k1.1
```

新包额外依赖 OpenCV 与 `libpython3.12t64`，一般由 apt 从 2603 一并装上。若模拟里要把向量客户端换成别的版本，停下来把完整输出发回来，不要继续。

装完按第 7 节拉起进程并跑 Embedding 烟测。向量客户端不要动。

### 6.2 不解包进系统，先对比符号

```bash
mkdir -p ~/mokoa-kylin-pkgs/extract-runtime-new
dpkg-deb -x kylin-ai-runtime_*.deb ~/mokoa-kylin-pkgs/extract-runtime-new
BIN=~/mokoa-kylin-pkgs/extract-runtime-new/usr/bin/kylin-ai-runtime

echo "=== NEEDED ==="
objdump -p "$BIN" | grep NEEDED

echo "=== ConnectParam relocations ==="
nm -D "$BIN" | grep ConnectParam
```

判定：

| runtime 的 ConnectParam 引用 | 应对 |
|---|---|
| 有 `C1Ev`（无参） | 旧 runtime；不能配本机 0k1.1 客户端 |
| 无 `C1Ev`，有带 string 的构造 | 新 runtime；可试方案 A |
| 两个都有 | 看实际调用；仍以能否和 0k1.1 动态链接为准 |

对客户端 `.deb` 同样解包后 `nm -D`。只有 **runtime 未定义符号 ⊆ client 已导出符号** 才允许拿去装。

还要核对：

```bash
file "$BIN"                          # ELF 64-bit LSB, x86-64
readelf -l "$BIN" | grep interpreter # 应为 /lib64/ld-linux-x86-64.so.2
# 若 readelf 显示需要 GLIBC_2.39+ 而本机是 2.38，这个包不能用
objdump -T "$BIN" | grep -o 'GLIBC_[0-9.]*' | sort -u
```

### 6.3 方案 A：先私有前缀试跑，再考虑 dpkg

把新 runtime 解到前缀目录，**继续使用系统里已经在用的新客户端**：

```bash
PREFIX=$HOME/mokoa-kylin-pkgs/extract-runtime-new
# 不要设置 LD_LIBRARY_PATH 指向旧 client
"$PREFIX/usr/bin/kylin-ai-runtime"
```

成功标志（约数秒内）：

```bash
ls -l /tmp/.kylin-ai-runtime-unix/$(id -u)/core-textembedding.sock
pgrep -af kylin-ai-runtime
```

若进程起来但立刻退出，看它自己的日志或 `dmesg` 无意义时直接看 stderr。常见下一问题是缺同代数据文件、模型或另一个 socket，那是依赖没下全，不是 ABI 没对上。

私有前缀试跑通过后，再用 **同一批 .deb** 安装，避免「试跑的是 A 版本、装上的是 B 版本」：

```bash
sudo dpkg -i kylin-ai-runtime_目标版本_amd64.deb
# 若 Depends 不满足：sudo apt-get -f install
# 或把 depends 的 .deb 一起 dpkg -i
```

装完再跑第 7 节。向量客户端包不要一起降级。

### 6.4 方案 B：旧 client 只给旧 runtime 用

```bash
dpkg-deb -x libkysdk-vector-engine-client_旧版本.deb ~/mokoa-kylin-pkgs/extract-runtime-old
OLD_LIB=$HOME/mokoa-kylin-pkgs/extract-runtime-old/usr/lib/x86_64-linux-gnu

nm -D "$OLD_LIB/libkysdk-vector-engine-client.so.1" | grep '_ZN8VectorDB12ConnectParamC1Ev'
# 必须能看到 T 或 W（已导出），不能是空

LD_LIBRARY_PATH="$OLD_LIB" /usr/bin/kylin-ai-runtime
```

确认 `ldd` 在该环境下解析到的是私有库，而不是 `/usr/lib/...`：

```bash
LD_LIBRARY_PATH="$OLD_LIB" ldd /usr/bin/kylin-ai-runtime | grep vector-engine-client
```

即使 embedding socket 出来了，也不要把这个旧 `.so` 装进系统路径。长期仍应换成方案 A。

### 6.5 做成可交付的离线包清单

`NOTES.md` 里至少写清：

```text
target: Kylin V11 2603 amd64 glibc 2.38 uid=1000
keep:   libkysdk-vector-engine-client 1.2.0.0-0k1.1
keep:   kylin-ai-vector-engine 1.2.0.1-0k0.11
change: kylin-ai-runtime 1.1.0.1-0k1.32  ->  1.2.0.4-0k0.1
sha256: 7126f18bf4e1af043eaea378591cecb93dc78897cd8d58163cc0f53eeb0f22d8
nm:     runtime 不再 U ConnectParamC1Ev；现为 U ConnectParam(std::string)
nm:     client 仍无 C1Ev，仍有 appId 构造
next:   1.2.0.4-0k0.1 仍缺 triton::client::InferenceServerHttpClient::Create
smoke:  embedding socket + python scripts/smoke_kylin_embedding.py
smoke:  python scripts/smoke_kylin_vector.py verify 仍然通过
```

把 `.deb` 和 `NOTES.md` 一起留在 `~/mokoa-kylin-pkgs/`。不要把系统 `.so` 提交进 MOKOA git。

## 7. 对齐之后必须过的检查

全部在麒麟 V11 宿主、项目 venv、**不要**设 BGE/local。

```bash
# 1) runtime 能解析符号
/usr/bin/kylin-ai-runtime --help >/tmp/mokoa-ai-runtime-help.txt 2>&1 || true
# 不应再出现 undefined symbol ConnectParamC1Ev

# 2) 若没有 systemd 单元，前台/后台拉起均可，只要 socket 出现
mkdir -p /tmp/mokoa-ai
# 按实际可用方式启动；成功后：
ls -l /tmp/.kylin-ai-runtime-unix/1000/core-textembedding.sock

# 3) Embedding 烟测
cd ~/projects/MOKOA
source .venv/bin/activate
export OS_AGENT_KYLIN_STRICT=1
python scripts/smoke_kylin_embedding.py --runs 5 --output data/reports/kylin_embedding_after_pkg.json
# 要求：passed=true, backend=kylin-coreai, using_fallback=false, dimension=768

# 4) 向量库仍在（防止救 Embedding 时把 client 弄坏）
export OS_AGENT_KYLIN_VECTOR_LIBRARY="$PWD/build/kylin-vector/libmokoa_kylin_vector_bridge.so"
export OS_AGENT_KYLIN_VECTOR_ENDPOINT=unix:/tmp/kylin-ai-vector-engine-1000.sock
python scripts/smoke_kylin_vector.py verify --manifest data/kylin-vector-smoke.json
# 若集合已清理，改为 write 再 verify

# 5) 再跑完整验收
export OS_AGENT_KYLIN_ACCEPTANCE=1
bash scripts/kylin_v11_full_acceptance.sh
```

`kylin-ai-runtime` 在这台机器上 **没有** systemd unit（已查过 `kylin-ai-vector-engine.service` 有、runtime 没有）。包对齐后仍可能要手动启动或开一次系统 AI 助手。那是服务拉起方式问题；只有 `undefined symbol` 消失才算 ABI 处理完成。

### 7.1 ConnectParam 已对齐后的下一缺口（2026-09-07 15:59）

runtime 已升到 `1.2.0.4-0k0.1`，向量客户端仍为 `1.2.0.0-0k1.1`。`ConnectParam` 已变为 `U ConnectParam(std::string)`，与本机 client 导出一致。

`--help` 的新错误是：

```text
undefined symbol:
_ZN6triton6client25InferenceServerHttpClient6CreateE...
```

解码后是 `triton::client::InferenceServerHttpClient::Create(...)`（带 `HttpSslOptions` 和 `bool`）。这是 NVIDIA Triton HTTP 客户端，**不是** MOKOA、也不是向量 `ConnectParam`。socket 仍不会出现，因为进程在动态链接阶段就退出了。

在宿主上查是缺包还是加载了错误版本的 `.so`：

```bash
ldd /usr/bin/kylin-ai-runtime
ldd /usr/bin/kylin-ai-runtime | grep -E 'not found|triton|httpclient'

objdump -p /usr/bin/kylin-ai-runtime | grep NEEDED

dpkg -L kylin-ai-runtime | grep -Ei 'triton|httpclient|\.so'

dpkg -l | grep -Ei 'triton|httpclient'
apt-cache search triton | head -40

find /usr /opt /lib -iname '*triton*' -o -iname '*httpclient*' 2>/dev/null | head -50
```

**2026-09-07 16:03：同一函数、参数顺序不同。拥有者是 `kytensor-client`。**

`/usr/lib/x86_64-linux-gnu/libhttpclient.so` 属于 `kytensor-client:amd64`；`/lib/...` 与它是同一 inode（硬链接，2026-02-12，215128 字节）。`dpkg -S /lib/...` 会报「没有找到路径」，不要用那一行的 `cut` 当包名。

| 来源 | `Create` 参数（url 之后） |
|---|---|
| 已装 `libhttpclient.so` | `(unique_ptr*, string, **bool**, **HttpSslOptions const&**)` |
| runtime `1.2.0.4-0k0.1` 需要 | `(unique_ptr*, string, **HttpSslOptions const&**, **bool**)` |

已导出符号：

```text
T ...CreateE...string...EbRKNS0_14HttpSslOptionsE
```

runtime 要的是：

```text
U ...CreateE...string...ERKNS0_14HttpSslOptionsEb
```

`NEED missing` 因此成立。`kylin-ai-runtime` 的 Depends 未声明 `kytensor-client`，apt 升级 runtime 时不会自动带上匹配的客户端。

**已装：** `kytensor-client` **`2.49.0.6-ok7k0.12`**（只在 dpkg status，pin 100）。**2603 候选：** **`2.49.0.6-ok7k0.14`**。

| 项 | 值 |
|---|---|
| `.deb` | `pool/main/k/kytensor/kytensor-client_2.49.0.6-ok7k0.14_amd64.deb` |
| SHA256 | `7d19b9fad15bdaa35ab7fad719cfd3ddfda2a3658d1f14f2278101d4f79e0781` |
| 来源 | `11.0-kylin-2603/main` |
| Depends 额外点 | `onnxruntime-backend`、`kytensor-python`、`llm-backend`（0.12 已有，一般已满足） |

先核符号（通过条件：出现 `HttpSslOptions` 在 `bool` **前面** 的 `Create`，即 mangling 含 `RKNS0_14HttpSslOptionsEb`）：

若 `apt-get download` 报 `502 Bad Gateway [IP: 127.0.0.1 7897]`，是本机代理拦了麒麟源，不是包不存在。可临时绕过代理再下：

```bash
cd ~/mokoa-kylin-pkgs/apt-cache
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u all_proxy -u ALL_PROXY \
  apt-get download kytensor-client=2.49.0.6-ok7k0.14
```

```bash
cd ~/mokoa-kylin-pkgs/apt-cache
apt-get download kytensor-client=2.49.0.6-ok7k0.14
sha256sum kytensor-client_2.49.0.6-ok7k0.14_amd64.deb

mkdir -p ~/mokoa-kylin-pkgs/extract-kytensor-new
dpkg-deb -x kytensor-client_2.49.0.6-ok7k0.14_amd64.deb ~/mokoa-kylin-pkgs/extract-kytensor-new
SO=~/mokoa-kylin-pkgs/extract-kytensor-new/usr/lib/x86_64-linux-gnu/libhttpclient.so
ls -l "$SO"
nm -D "$SO" | grep InferenceServerHttpClient | grep Create
```

`nm` 通过后再看模拟安装（不应降级 `libkysdk-vector-engine-client`）：

```bash
apt-get -s install kytensor-client=2.49.0.6-ok7k0.14
```

**2026-09-07 18:13：三包已对齐，runtime 主进程已启动。**

| 包 | 版本 |
|---|---|
| `kylin-ai-runtime` | `1.2.0.4-0k0.1` |
| `kytensor-client` | `2.49.0.6-ok7k0.14` |
| `libkysdk-vector-engine-client` | `1.2.0.0-0k1.1`（未改） |

`--help` 实际进入了主循环：`Starting CoreTextEmbedding server`、`All servers started`。百度/讯飞配置格式报错可忽略。仍失败的是端侧插件，缺的是另一套 Triton 符号：

- `libkylin-ondevice-vision-engine.so`：旧 HTTP `Create(..., bool, HttpSslOptions)`
- `libkylin-ondevice-embedding-engine.so.1`：`InferenceServerGrpcClient::Create(...)`

MOKOA 走的是 CoreAI socket，不一定依赖这两个插件。先看 socket 再决定要不要升 ondevice 包。

**2026-09-07 18:21：socket 已通，`text_embedding_init_session` 仍失败。**

烟测已连上 `unix:path=/tmp/.kylin-ai-runtime-unix/1000/core-textembedding.sock`，协议为 runtime **1.3.0 / init_v2**（9 月 6 日成功时是 1.0.0 / init_v1）。随后 `Proxy init failed, errorCode: 3`。严格模式不降级。这是 runtime 侧 embedding 引擎没初始化成功，不是 MOKOA 连错 socket。最可疑的是启动时没加载上的 `libkylin-ondevice-embedding-engine.so.1`（缺 `InferenceServerGrpcClient::Create`）。

```bash
dpkg -S /usr/lib/x86_64-linux-gnu/libkylin-ondevice-embedding-engine.so.1 \
        /usr/lib/x86_64-linux-gnu/libgrpcclient.so
ls -l /usr/lib/x86_64-linux-gnu/libgrpcclient.so

NEED_GRPC=_ZN6triton6client25InferenceServerGrpcClient6CreateEPSt10unique_ptrIS1_St14default_deleteIS1_EERKNSt7__cxx1112basic_stringIcSt11char_traitsIcESaIcEEEbbRKNS0_10SslOptionsERKNS0_16KeepAliveOptionsEb
nm -D /usr/lib/x86_64-linux-gnu/libgrpcclient.so | grep InferenceServerGrpcClient | grep Create
nm -D /usr/lib/x86_64-linux-gnu/libgrpcclient.so | grep "$NEED_GRPC" || echo "NEED_GRPC missing"

apt-cache policy libkylin-ondevice-embedding-engine libkylin-ondevice-vision-engine
apt-cache madison libkylin-ondevice-embedding-engine
```

**2026-09-07 20:27：Grpc `Create` 差一个尾部 `bool`；2603 候选为 `1.2.0.0-0k1.0`。**

`libgrpcclient.so` 属于已升的 `kytensor-client 0.14`。它导出的是 `Create(..., KeepAliveOptions, bool, bool)`（mangling 以 `OptionsEbb` 结尾）。已装 embedding 引擎 `0k0.10` 要的是少一个 `bool` 的 `OptionsEb`。vision 同样停在旧版本（已装 `0k0.12`，候选 `0k1.0`）。先升 **embedding**（MOKOA 需要），vision 可随后一起升以免再挡插件。

```bash
cd ~/mokoa-kylin-pkgs/apt-cache
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u all_proxy -u ALL_PROXY \
  apt-get download libkylin-ondevice-embedding-engine=1.2.0.0-0k1.0

mkdir -p ~/mokoa-kylin-pkgs/extract-ondevice-embed
dpkg-deb -x libkylin-ondevice-embedding-engine_1.2.0.0-0k1.0_amd64.deb \
  ~/mokoa-kylin-pkgs/extract-ondevice-embed
ENG=~/mokoa-kylin-pkgs/extract-ondevice-embed/usr/lib/x86_64-linux-gnu/libkylin-ondevice-embedding-engine.so.1
nm -D "$ENG" | grep InferenceServerGrpcClient | grep Create
```

通过条件：`U` 符号以 `KeepAliveOptionsEbb` 结尾（与当前 `libgrpcclient.so` 的 `T` 一致），不再是少一个 `b` 的旧签名。

**2026-09-07 20:44：干净重启后 embedding 引擎已能 `Proxy init success`。**

进程只剩一个，socket 时间为 20:43。日志无 `ondevice-embedding` 缺符号，仅 vision 仍失败。烟测连上 1.3.0/`init_v2` 后会话初始化成功，失败变为：

```text
ModelInfo format error
Get default model failed: getModelList failed, errorCode: 3
text_embedding failed: code=3, message=Ensure default model failed.
```

**2026-09-07 20:48：模型仓库里已有 GTE 文本模型；C SDK 无更新候选。**

`libkylin-coreai-embedding` 已是 `1.2.0.0-0k0.4`。`/usr/share/kylin-ai/model-repository` 含 `ensemble-embd_gte-base_uint8-text`（应对齐此前 768 维）。`.so` 已导出 `text_embedding_init_model` / `text_embedding_get_model_list`，但 `embedding.h` 不在 `/usr/include/coreai/embedding/`。`~/.config/kylin-ai-runtime/modelinfo/` 只有 `freetrial_model_info.json`，默认 `getModelList` 因此格式失败。**2026-09-07 20:50：其余 ondevice 插件均有 2603 `0k1.0` 候选；C API 已明确 `text_embedding_init_model`。**

头文件：`/usr/include/kylin-ai/coreai/embedding/embedding.h`。`init_session` 之后必须 `text_embedding_init_model(session, name)`。MOKOA 已钉 `ensemble-embd_gte-base_uint8-text`（可用 `OS_AGENT_KYLIN_EMBEDDING_MODEL` 覆盖）。

**2026-09-07 21:58：Embedding 烟测通过。**

`scripts/smoke_kylin_embedding.py`：`passed=true`，`backend=kylin-coreai`，`using_fallback=false`，**768 维**，相关/无关相似度 0.859 / 0.389，中位延迟约 20ms。`Proxy init model success, model: ensemble-embd_gte-base_uint8-text`。正式名以 `get_model_list` 为准。kytensor + runtime 需保持运行才能做完整验收。

当前 `ok4` 已把 `libtriton_onnxruntime.so` 装在 `/opt/tritonserver/backends/onnxruntime/`，但 kytensor 仍找不到，需 `ok6k0.1`。`model_bank` 包已装 `1.2.0.1-0k0.8`，候选 `0k1.0`。`kytensor.conf` 仅有 `KYTENSOR_PROVIDER_ON=0`。两包一起升，勿 `autoremove`。

- `libkylin-ondevice-ai-engine-plugin`（已装 `0k0.4`）
- `libkylin-ondevice-traditional-ai-engine-plugin`（已装 `0k0.5`，注释写明服务 ondevice embedding）
- `libkylin-ondevice-nlp-engine`（已装 `0k0.16`）
- `libkylin-ondevice-vision-engine`（已装 `0k0.12`）

## 8. 和 MOKOA 的边界

| 可以动 | 不要动 |
|---|---|
| 成套安装/升级 `kylin-ai-runtime` 及它声明的 Depends | `native/kylin_vector_bridge/bridge.cpp`（已按新 client 锁定） |
| 离线目录里隔离旧 client 做对照实验 | 把旧 client 覆盖到 `/usr/lib` |
| 为 runtime 补同代数据/模型/助手包 | `OS_AGENT_EMBEDDING_BACKEND=local` / BGE / hash |
| 装完后重跑烟测和 `kylin_v11_full_acceptance.sh` | 在 Cursor/KARE 里验证这些 `.so` |

ABI 对齐完成的定义只有一句：

**`/usr/bin/kylin-ai-runtime` 在本机默认库路径下能启动，并创建 `core-textembedding.sock`；同时 MOKOA 向量桥接仍能连上现有 `kylin-ai-vector-engine`。**
