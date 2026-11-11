#!/usr/bin/env python3
"""生成功能覆盖演示集：80 案例 Markdown + JSONL。"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "tests" / "fixtures" / "feature_coverage"
DOC = ROOT / "docs" / "功能覆盖演示集_80案例.md"
ATTACH_DIR = "tests/fixtures/feature_coverage/attachments"
OVERSIZE = "data/demo_coverage/attachments/10_oversized_dummy.pdf"

USER_A = "demo_user_a"
USER_B = "demo_user_b"
BG = "统一背景：用户在麒麟电脑上处理学习 / 办公 / 开发任务；主线虚构项目「星河笔记」(XingHe Notes)，对照项目「课程助手」(CourseAssistant)。"


def case(
    cid: str,
    func: str,
    *,
    scene: str,
    pre: str,
    steps: str,
    expect: str,
    forbid: str,
    user: str = USER_A,
    must: bool = False,
    attachment: str | None = None,
) -> dict:
    return {
        "id": cid,
        "function": func,
        "must": must,
        "user": user,
        "scene": scene,
        "precondition": pre,
        "steps": steps,
        "expected": expect,
        "forbidden": forbid,
        "attachment": attachment,
        "actual": "",
        "evidence": "",
        "pass_fail": "",
    }


CASES: list[dict] = []

# ---- SRC 多源接入 8 ----
CASES += [
    case("SRC-01", "多源接入", scene="coding", pre="API 已连接", steps="对话：请记住 pip 超时可换清华镜像", expect="conversation 源写入知识；accepted=true；source 记录为 chat/conversation", forbid="无 source 或写成 document"),
    case("SRC-02", "多源接入", scene="os_operation", pre="无", steps="POST /ingest source=tool_result，stderr=连接超时，建议切换国内镜像", expect="tool_result 接入成功；内容含 pip/超时/镜像", forbid="被当成 preference"),
    case("SRC-03", "多源接入", scene="coding", pre="无", steps="POST /ingest source=user_behavior，经常打开 VSCode，occurrence_count=5", expect="behavior 接入成功", forbid="无事件丢弃且无提示"),
    case("SRC-04", "多源接入", scene="global", pre="无", steps="POST /ingest source=manual_config，key=reply_language value=中文", expect="manual_config 写入；可在偏好/配置侧查到", forbid="覆盖用户 B"),
    case("SRC-05", "多源接入", scene="document_upload", pre="附件就绪", steps="上传 01_xinghe_notes_manual.pdf", expect="document 源成功；抽到 port 8080 旧手册事实", forbid="HTTP 非业务错误伪装成功", attachment="01_xinghe_notes_manual.pdf"),
    case("SRC-06", "多源接入", scene="document_upload", pre="无", steps="上传空文件 08_empty.pdf", expect="明确失败提示（空文件）；不写记忆", forbid="静默成功", attachment="08_empty.pdf"),
    case("SRC-07", "多源接入", scene="global", pre="无", steps="POST /ingest 缺 user_id 或空 user_id", expect="接口拒绝并给出可读错误", forbid="写入匿名脏数据"),
    case("SRC-08", "多源接入", scene="coding", pre="SRC-01 已写入", steps="同一轮查看右侧活动/记忆面板 source 字段", expect="各入口 source 可区分 conversation/tool/behavior/config/document", forbid="全部显示未知来源"),
]

# ---- DOC 文档与图片 10 ----
CASES += [
    case("DOC-01", "文档与图片", scene="document_upload", pre="无", steps="上传 01_xinghe_notes_manual.pdf → 问：星河笔记旧手册端口是多少？", expect="抽文本含 8080；回答指向旧手册 8080", forbid="直接答成 9090 且不说明来源版本", attachment="01_xinghe_notes_manual.pdf"),
    case("DOC-02", "文档与图片", scene="document_upload", pre="无", steps="上传 02_office_ops_guide.pdf → 问打印机共享名", expect="答 KYLIN-PRINT-01", forbid="编造其它打印机名", attachment="02_office_ops_guide.pdf"),
    case("DOC-03", "文档与图片", scene="document_upload", pre="无", steps="上传 03_meeting_minutes.docx → 问周会负责人与日期", expect="负责人李四/Li Si；日期 2026-09-05", forbid="张冠李戴", attachment="03_meeting_minutes.docx"),
    case("DOC-04", "文档与图片", scene="document_upload", pre="DOC-01 已上传旧手册", steps="上传 04_xinghe_config_update.docx → 问星河笔记当前端口", expect="以变更通知为准：9090；可说明曾为 8080", forbid="只答 8080 当作当前值", attachment="04_xinghe_config_update.docx"),
    case("DOC-05", "文档与图片", scene="document_upload", pre="无", steps="上传 05_terminal_error.png（可加 caption：端口占用）", expect="识别 Address already in use / 提示改用 9090；标明 vision 或 OCR/说明路径", forbid="无提示假装解析成功", attachment="05_terminal_error.png"),
    case("DOC-06", "文档与图片", scene="document_upload", pre="无", steps="上传 06_settings_page.png", expect="抽到 E:/Downloads 与 zh-CN 设定线索", forbid="写成 D 盘", attachment="06_settings_page.png"),
    case("DOC-07", "文档与图片", scene="document_upload", pre="无", steps="上传 07_ocr_scan_page.png（扫描件 OCR）", expect="健康检查 URL 含 9090；owner=Li Si；extract 路径可标 OCR", forbid="空白内容却 accepted", attachment="07_ocr_scan_page.png"),
    case("DOC-08", "文档与图片", scene="document_upload", pre="无", steps="上传 08_empty.pdf", expect="拒绝：空文件；前端有失败气泡", forbid="写入空知识", attachment="08_empty.pdf"),
    case("DOC-09", "文档与图片", scene="document_upload", pre="无", steps="上传 09_unsupported.xyz", expect="拒绝：不支持类型；提示仅 PDF/Word/图片", forbid="按文本强行入库", attachment="09_unsupported.xyz"),
    case("DOC-10", "文档与图片", scene="document_upload", pre="无", steps=f"上传 {OVERSIZE}（>8MB，先跑 fixtures 脚本生成）", expect="拒绝：超过 8MB 上限", forbid="截断后静默成功", attachment=OVERSIZE),
]

# ---- QC 清洗与质量控制 8 ----
CASES += [
    case("QC-01", "清洗与质量控制", scene="coding", pre="无", steps="摄入重复两遍的同一知识句", expect="有效内容保留；重复按去重规则处理（拒绝或合并）", forbid="产生两条完全重复长期事实"),
    case("QC-02", "清洗与质量控制", scene="global", pre="无", steps="摄入带大量空白/换行的文本", expect="清洗后保留有效句；不因空白拒识全部语义", forbid="原样落库大量空白噪声"),
    case("QC-03", "清洗与质量控制", scene="office", pre="无", steps="摄入含 HTML 标签的片段 <b>备份先开</b>", expect="去标签后保留语义", forbid="把标签当事实"),
    case("QC-04", "清洗与质量控制", scene="global", pre="无", steps="摄入无意义短句：嗯 / ok / 哈哈哈", expect="低质拒绝或不高置信写入长期知识", forbid="当成新事实知识"),
    case("QC-05", "清洗与质量控制", scene="coding", pre="无", steps="摄入正常有效知识：星河笔记日志在 /var/log/xinghe/notes.log", expect="accepted；可召回", forbid="被 QC 误杀"),
    case("QC-06", "清洗与质量控制", scene="global", pre="无", steps="只提问：今天天气怎么样？", expect="不当作新知识写入", forbid="把问题句存成事实"),
    case("QC-07", "清洗与质量控制", scene="coding", pre="无", steps="摄入「你记得我的下载目录吗？」类查询句", expect="不写入新的 download_path 偏好（与必做 PREF-08 同判据）", forbid="凭空生成目录偏好"),
    case("QC-08", "清洗与质量控制", scene="global", pre="无", steps="摄入混有广告口号的短文本", expect="按质量门拒绝或降权", forbid="广告句进入高置信知识"),
]

# ---- PREF 偏好提取与更新 12（含必做）----
CASES += [
    case("PREF-01", "偏好提取与更新", scene="os_operation", pre="干净用户", steps="以后下载文件默认保存到 D 盘", expect="habit.download_path≈D:/Downloads；面板可见", forbid="未提取却提示已保存"),
    case("PREF-02", "偏好提取与更新", scene="os_operation", pre="PREF-01", steps="改口：以后改到 E 盘下载 → 再问当前下载目录", expect="当前值为 E；旧 D 非当前答案；历史可查", forbid="仍答 D 为当前", must=False),
    case("PREF-03", "偏好提取与更新", scene="os_operation", pre="PREF-02", steps="改写提问：我的默认保存路径是哪里？", expect="仍召回 E 盘偏好", forbid="答无关路径"),
    case("PREF-04", "偏好提取与更新", scene="global", pre="无", steps="以后请用中文回答", expect="style.reply_language=zh-CN（或等价）", forbid="无偏好记录"),
    case("PREF-05", "偏好提取与更新", scene="coding", pre="无", steps="写代码时默认用 Python", expect="提取编程语言/工具偏好", forbid="写入下载路径"),
    case("PREF-06", "偏好提取与更新", scene="global", pre="无", steps="以后请用中文回答，下载文件默认保存到 E 盘", expect="同句抽出语言+下载两条偏好", forbid="只抽出一条就结束"),
    case("PREF-07", "偏好提取与更新", scene="global", pre="PREF-06", steps="【必做】忘掉下载目录 → 再查回复语言", expect="语言偏好仍在；下载偏好已忘", forbid="两条一起删", must=True),
    case("PREF-08", "偏好提取与更新", scene="os_operation", pre="无偏好或已清", steps="【必做】只问：你记得我的下载目录吗？", expect="纯查询；不新写入 download_path", forbid="因提问生成目录偏好", must=True),
    case("PREF-09", "偏好提取与更新", scene="office", pre="已有 E 盘偏好", steps="打开偏好面板看版本历史", expect="可见版本链/当前标记", forbid="历史空白却声称有更新"),
    case("PREF-10", "偏好提取与更新", scene="global", pre="无", steps="回答请更简洁", expect="风格类偏好更新", forbid="误改下载目录"),
    case("PREF-11", "偏好提取与更新", scene="coding", pre="PREF-05", steps="改口：以后默认用 Go", expect="新值覆盖当前；可追溯旧 Python", forbid="双当前值并存且无版本"),
    case("PREF-12", "偏好提取与更新", scene="os_operation", pre="无", steps="手动配置入口写入 download_path=D:/Downloads 后再对话改为 E", expect="最终当前 E；source 可区分", forbid="manual 与 chat 互相不可见导致双真相"),
]

# ---- KNOW 知识检索 12 ----
CASES += [
    case("KNOW-01", "知识检索", scene="coding", pre="已写入 pip 清华镜像知识", steps="原话问：pip 超时怎么办？", expect="答到换清华/国内镜像", forbid="答下载目录"),
    case("KNOW-02", "知识检索", scene="coding", pre="KNOW-01", steps="改写：装包很慢有什么办法？", expect="仍命中镜像知识", forbid="答非相关办公打印机"),
    case("KNOW-03", "知识检索", scene="coding", pre="DOC-04", steps="星河笔记健康检查地址？", expect="127.0.0.1:9090/health", forbid="只给 8080"),
    case("KNOW-04", "知识检索", scene="office", pre="DOC-02", steps="会议室访客 WiFi？", expect="Kylin-Office-Guest", forbid="编造 SSID"),
    case("KNOW-05", "知识检索", scene="coding", pre="DOC-03", steps="星河笔记周会谁负责？", expect="李四 / Li Si", forbid="张三（旧手册 Owner 混淆为当前负责人）"),
    case("KNOW-06", "知识检索", scene="coding", pre="已有下载偏好 E + pip 知识", steps="【必做】问：pip 镜像怎么配？", expect="只返回镜像相关知识；不把下载偏好拼进答案主结论", forbid="答案里夹带 E 盘下载偏好", must=True),
    case("KNOW-07", "知识检索", scene="global", pre="有若干记忆", steps="问：明天股价多少？", expect="无相关则明确没有/不编造项目事实", forbid="硬凑星河笔记端口"),
    case("KNOW-08", "知识检索", scene="coding", pre="DOC-01+DOC-04", steps="星河笔记现在用哪个端口？", expect="9090（变更后）", forbid="8080 作为当前唯一答案"),
    case("KNOW-09", "知识检索", scene="coding", pre="DOC-04", steps="课程助手端口是多少？", expect="8080 且归属 CourseAssistant", forbid="说成星河笔记当前端口"),
    case("KNOW-10", "知识检索", scene="office", pre="DOC-02", steps="WPS 如何导出 PDF？", expect="File -> Export -> PDF 或等价", forbid="无关下载偏好"),
    case("KNOW-11", "知识检索", scene="coding", pre="QC-05", steps="星河笔记日志路径？", expect="/var/log/xinghe/notes.log", forbid="空召回却假装记得"),
    case("KNOW-12", "知识检索", scene="coding", pre="DOC-05", steps="终端报 Address already in use 该怎么办？", expect="提示端口冲突；星河笔记应改 9090", forbid="建议随意杀进程且不提端口事实"),
]

# ---- CONF 冲突处理 8 ----
CASES += [
    case("CONF-01", "冲突处理", scene="coding", pre="旧手册 8080 已入库", steps="再写入变更通知 9090 → 问当前端口", expect="更新生效为 9090", forbid="两值同等当前无区分"),
    case("CONF-02", "冲突处理", scene="coding", pre="CONF-01", steps="问：以前端口是多少？", expect="可答曾为 8080 或从历史可见；不覆盖成当前", forbid="否认曾存在 8080"),
    case("CONF-03", "冲突处理", scene="coding", pre="星河 9090 + 课程助手 8080", steps="分别问两项目端口", expect="并存：星河 9090 / 课程助手 8080", forbid="全局只保留一个端口"),
    case("CONF-04", "冲突处理", scene="os_operation", pre="D 盘偏好", steps="改为 E 盘 → 查当前", expect="更新；旧版非当前", forbid="随机回滚 D"),
    case("CONF-05", "冲突处理", scene="coding", pre="同一事实重复写入两次相同内容", steps="写入两次相同日志路径", expect="去重/不双写垃圾", forbid="冲突覆盖掉其它项目事实"),
    case("CONF-06", "冲突处理", scene="coding", pre="负责人李四", steps="写入错误事实负责人王五（低置信）再高置信确认李四", expect="高置信/新确认保留李四", forbid="被低质噪声永久覆盖"),
    case("CONF-07", "冲突处理", scene="office", pre="打印机 KYLIN-PRINT-01", steps="写入另一办公室打印机 KYLIN-PRINT-02（不同场景/地点）", expect="可并存或按场景隔离", forbid="错误覆盖唯一打印机"),
    case("CONF-08", "冲突处理", scene="coding", pre="DOC 全套", steps="连续问答：星河端口 → 课程助手端口 → 再问星河", expect="不被上一轮答案串台", forbid="第二次把星河答成 8080"),
]

# ---- SEC 敏感信息与遗忘 10 ----
CASES += [
    case("SEC-01", "敏感信息与遗忘", scene="global", pre="无", steps="请记住测试手机号 13800138000（标注虚拟号）", expect="按安全策略拦截或脱敏入库", forbid="明文长期可召回展示"),
    case("SEC-02", "敏感信息与遗忘", scene="global", pre="无", steps="测试邮箱 demo_user_a@example.test", expect="脱敏或受控存储", forbid="明文出现在前端气泡"),
    case("SEC-03", "敏感信息与遗忘", scene="os_operation", pre="已有 E 盘偏好", steps="忘掉关于下载目录的偏好 → 再问下载目录", expect="不再召回 E 盘为当前偏好", forbid="遗忘失败仍答 E"),
    case("SEC-04", "敏感信息与遗忘", scene="os_operation", pre="SEC-03 + 曾用对话写入过目录", steps="【必做】再次问下载目录", expect="不应从原聊天历史把旧目录捞回当偏好答案", forbid="遗忘后历史对话又当偏好生效", must=True),
    case("SEC-05", "敏感信息与遗忘", scene="coding", pre="已有 pip 镜像知识", steps="忘掉 pip 镜像相关知识 → 再问 pip 超时", expect="不再返回该条；可提示无相关记忆", forbid="还能原样召回"),
    case("SEC-06", "敏感信息与遗忘", scene="global", pre="PREF-06 双偏好", steps="只遗忘下载目录", expect="语言偏好保留（同 MUST/PREF-07）", forbid="误删语言偏好"),
    case("SEC-07", "敏感信息与遗忘", scene="global", pre="无", steps="遗忘指令空提交", expect="前端/后端提示需输入；不误删", forbid="清空全部记忆"),
    case("SEC-08", "敏感信息与遗忘", scene="coding", pre="多条知识", steps="针对性遗忘星河笔记端口知识", expect="仅相关删除；办公打印机知识仍在", forbid="误伤其它项目"),
    case("SEC-09", "敏感信息与遗忘", scene="global", pre="SEC-01", steps="尝试召回手机号", expect="无明文或策略拒绝", forbid="完整号码回显"),
    case("SEC-10", "敏感信息与遗忘", scene="os_operation", pre="遗忘成功", steps="查看活动流/遗忘结果面板", expect="显示删除数量与 ID；前端不伪造成功", forbid="失败却显示已删除"),
]

# ---- ISO 用户与场景隔离 6 ----
CASES += [
    case("ISO-01", "用户与场景隔离", scene="os_operation", pre=f"{USER_A}=E 盘；{USER_B}=D 盘", steps=f"切换用户到 {USER_B} 问下载目录", expect="答 D；不可见 A 的 E", forbid="串到 E", user=USER_B),
    case("ISO-02", "用户与场景隔离", scene="os_operation", pre="ISO-01", steps=f"切回 {USER_A} 问下载目录", expect="仍为 E", forbid="被 B 覆盖", user=USER_A),
    case("ISO-03", "用户与场景隔离", scene="study", pre=f"{USER_A} 在 study 写入：复习用 Anki", steps="在 office 场景问学习工具", expect="按场景规则：隔离或不串台到办公答案", forbid="办公场景把 Anki 当办公默认"),
    case("ISO-04", "用户与场景隔离", scene="office", pre="ISO-03", steps="office 写入打印机事实后，study 问打印机", expect="按设计隔离或显式说明跨场景", forbid="静默污染"),
    case("ISO-05", "用户与场景隔离", scene="coding", pre=f"{USER_A} 有星河 9090", steps=f"{USER_B} 问星河端口", expect="B 无该知识则不能答出 A 的 9090", forbid="跨用户泄漏", user=USER_B),
    case("ISO-06", "用户与场景隔离", scene="document_upload", pre="无", steps=f"{USER_B} 上传办公指南后，{USER_A} 未上传前不应已有该文档记忆", expect="文档记忆按 user_id 隔离", forbid="全局共享脏读", user=USER_A),
]

# ---- FLOW 记忆流转与持久化 6 ----
CASES += [
    case("FLOW-01", "记忆流转与持久化", scene="coding", pre="干净会话", steps="写入一条短期知识后立刻看分层统计", expect="short 层计数增加（或符合实现）", forbid="无任何层变化却提示已写入"),
    case("FLOW-02", "记忆流转与持久化", scene="coding", pre="FLOW-01", steps="触发 consolidate / 晋升", expect="可进入长期/向量层；随后 /recall 可命中", forbid="晋升失败却显示成功"),
    case("FLOW-03", "记忆流转与持久化", scene="coding", pre="已有长期知识", steps="重复访问同一问题 3 次", expect="稳定召回；不因重复访问复制垃圾条", forbid="条数暴涨"),
    case("FLOW-04", "记忆流转与持久化", scene="global", pre="已有偏好 E + 知识 9090", steps="重启 API 服务后再问端口与下载目录", expect="持久化内容仍可召回", forbid="重启后全部丢失"),
    case("FLOW-05", "记忆流转与持久化", scene="coding", pre="仅短期未晋升", steps="重启后只依赖短期的内容", expect="行为符合实现说明（丢失或仍在）并记录实际结果", forbid="文档与实现完全相反且无记录"),
    case("FLOW-06", "记忆流转与持久化", scene="document_upload", pre="DOC-04 已晋升", steps="重启 → 再问星河端口", expect="仍答 9090", forbid="回退成无记忆"),
]

assert len(CASES) == 80, len(CASES)
assert sum(1 for c in CASES if c["must"]) == 4


def md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", "<br>")


def render_md() -> str:
    must_ids = [c["id"] for c in CASES if c["must"]]
    lines = [
        "# 功能覆盖演示集（80 案例）",
        "",
        "> 用途：完整测试可跑 80 条；正式演示从中抽 8–12 步串成一条故事线。",
        "> 材料口径：写明样本量、功能范围、实际结果即可，不必包装成「大规模评测」。",
        "",
        BG,
        "",
        "## 1. 规模与范围",
        "",
        "| 功能 | 案例数 | ID 前缀 |",
        "|---|---:|---|",
        "| 多源接入 | 8 | SRC |",
        "| 文档与图片 | 10 | DOC |",
        "| 清洗与质量控制 | 8 | QC |",
        "| 偏好提取与更新 | 12 | PREF |",
        "| 知识检索 | 12 | KNOW |",
        "| 冲突处理 | 8 | CONF |",
        "| 敏感信息与遗忘 | 10 | SEC |",
        "| 用户与场景隔离 | 6 | ISO |",
        "| 记忆流转与持久化 | 6 | FLOW |",
        "| **合计** | **80** | |",
        "",
        "### 浏览器实况必做（4）",
        "",
        f"已编入：`{'` `'.join(must_ids)}`",
        "",
        "1. **PREF-08 / QC-07**：问「你记得我的下载目录吗？」只查询，不生成新目录偏好。",
        "2. **KNOW-06**：问 pip 镜像时，不把下载偏好拼进答案。",
        "3. **SEC-04**：遗忘下载目录后，不能从原聊天历史再捞回旧目录当偏好。",
        "4. **PREF-07 / SEC-06**：同句「中文 + E 盘」写入后，只忘下载目录，语言偏好保留。",
        "",
        "## 2. 附件清单（10）",
        "",
        f"目录：`{ATTACH_DIR}/`（`python3 scripts/generate_feature_coverage_fixtures.py` 可重生；超大文件在 `data/`）",
        "",
        "| 文件 | 用途 | 标准答案要点 |",
        "|---|---|---|",
        "| `01_xinghe_notes_manual.pdf` | 旧项目手册 | XingHe Notes **port 8080**（旧） |",
        "| `02_office_ops_guide.pdf` | 办公操作 | 打印机 `KYLIN-PRINT-01`；WiFi `Kylin-Office-Guest` |",
        "| `03_meeting_minutes.docx` | 会议纪要 | Owner **Li Si**；Date **2026-09-05** |",
        "| `04_xinghe_config_update.docx` | 配置变更 | XingHe Notes **port 9090**；CourseAssistant 仍 **8080** |",
        "| `05_terminal_error.png` | 终端报错图 | `Address already in use`；提示改用 9090 |",
        "| `06_settings_page.png` | 设置页截图 | `E:/Downloads`；`zh-CN` |",
        "| `07_ocr_scan_page.png` | OCR 扫描页 | health `9090`；owner Li Si |",
        "| `08_empty.pdf` | 异常-空文件 | 拒绝 |",
        "| `09_unsupported.xyz` | 异常-类型 | 拒绝 |",
        f"| `{OVERSIZE}` | 异常-超 8MB（gitignore） | 拒绝 |",
        "",
        "## 3. 正式演示串联（12 步，可剪到 8）",
        "",
        "| 步 | 案例 | 操作 | 看点 |",
        "|---:|---|---|---|",
        "| 1 | SRC-01 | 对话写入 pip 镜像知识 | 多源-对话入口 |",
        "| 2 | PREF-06 | 「中文回答 + 下载到 E 盘」 | 同句多偏好 |",
        "| 3 | PREF-08 | 问「你记得我的下载目录吗？」 | **必做：查询不写偏好** |",
        "| 4 | DOC-01→DOC-04 | 先旧手册 8080，再变更通知 9090 | 文档抽取 + 冲突更新 |",
        "| 5 | KNOW-08 / KNOW-09 | 分别问星河 / 课程助手端口 | 项目隔离并存 |",
        "| 6 | KNOW-06 | 问 pip 镜像 | **必做：不拼下载偏好** |",
        "| 7 | DOC-08/09/10 | 空/不支持/超大 | 异常提示 |",
        "| 8 | PREF-07 | 忘掉下载目录，再查语言 | **必做：部分遗忘** |",
        "| 9 | SEC-04 | 再问下载目录 | **必做：历史不回潮** |",
        "| 10 | ISO-01/02 | 切换 user_b / user_a | 用户隔离 |",
        "| 11 | FLOW-02 | 晋升后召回 | 记忆流转 |",
        "| 12 | FLOW-04 | 重启服务再问 | 持久化 |",
        "",
        "## 4. 案例明细（每条含标准答案）",
        "",
        "记录时请填「实际结果 / 证据 / 通过」三列；证据可为返回原文、memory_id、截图路径。",
        "",
    ]

    current = None
    for c in CASES:
        if c["function"] != current:
            current = c["function"]
            lines += [f"### {current}", ""]
            lines += [
                "| 案例编号 | 用户/场景/前置 | 输入与步骤 | 期望结果 | 禁止结果 | 附件 | 必做 | 实际结果 | 证据 | 通过 |",
                "|---|---|---|---|---|---|---|---|---|---|",
            ]
        lines.append(
            "| "
            + " | ".join(
                [
                    f"**{c['id']}**",
                    md_escape(f"{c['user']} / {c['scene']} / {c['precondition']}"),
                    md_escape(c["steps"]),
                    md_escape(c["expected"]),
                    md_escape(c["forbidden"]),
                    c.get("attachment") or "-",
                    "是" if c["must"] else "-",
                    "",
                    "",
                    "",
                ]
            )
            + " |"
        )
    lines += [
        "",
        "## 5. 执行说明",
        "",
        "1. 启动后端与前端（或 `scripts/kylin_v11_demo_start.sh`），用户框使用 `demo_user_a`。",
        f"2. 附件从 `{ATTACH_DIR}/` 上传；DOC-10 使用 `{OVERSIZE}`。",
        "3. 建议分批：SRC+DOC → PREF+KNOW → CONF+SEC → ISO+FLOW。",
        f"4. 机器可读副本：`{OUT_DIR.relative_to(ROOT) / 'cases_80.jsonl'}`。",
        "5. `security_index.json` 等运行时敏感文件不要入库。",
        "",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    DOC.parent.mkdir(parents=True, exist_ok=True)
    DOC.write_text(render_md(), encoding="utf-8")
    with (OUT_DIR / "cases_80.jsonl").open("w", encoding="utf-8") as fh:
        for c in CASES:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"wrote {DOC}")
    print(f"wrote {OUT_DIR / 'cases_80.jsonl'} count={len(CASES)} must={sum(1 for c in CASES if c['must'])}")


if __name__ == "__main__":
    main()
