"""LLM prompts for preference extraction (zh/en)."""

from __future__ import annotations

from src.preference.keys import PREFERENCE_KEYS

ALLOWED_KEYS_TEXT = "\n".join(f"- {k}" for k in sorted(PREFERENCE_KEYS))

EXTRACTION_SYSTEM_PROMPT = """You are an OS Agent preference extraction engine for Kylin desktop OS.
Extract DURABLE structured preferences from user utterances.

ALWAYS extract when the utterance implies a reusable policy, even if phrased as a current task.
Extract ALL applicable keys (often 2+), not just one.

Positive signals → extract:
- Path / D盘 / E盘 / downloads → habit.download_path
- 项目文件夹 / 按项目 → habit.file_naming_rule=project_prefix
- 整理成表格 / Excel → tool_selection.data_processing_tool=excel (scene office)
- 直接删 / 不用问 → security.confirm_before_rm=false AND workflow.preview_before_execute=false
- 搜文件/合同 → tool_selection.file_search_tool=everything
- 用英文 + 报告 → style.reply_language=en-US AND style.report_format (docx|pdf)
- 按类型分类 → habit.file_organize_rule=by_type; if organizing/moving many files also workflow.backup_before_modify=true
- 压缩包 / zip → habit.archive_format=zip; 密码/加密 → security.confirm_before_highrisk=true
- Word转PDF / 发给客户 → style.report_format=pdf AND workflow.auto_complete_steps=true
- 批量重命名 + 按日期 → habit.file_naming_rule=date_prefix AND tool_selection.batch_operation_tool=bash
- 合同改日期/批量改文本 → tool_selection.text_processing_tool=python AND workflow.backup_before_modify=true
- 喜欢/爱/爱吃/讨厌/不喜欢 + 具体事物 → habit.likes 或 habit.dislikes（顿号分隔；问句不抽取）
- 无法映射到上述任一键的持久偏好（如「写代码默认用 Go」「报告默认用蓝色主题」）→ 归入 custom 键：
  category 取 habit/style/security/tool_selection/workflow/communication 之一，
  key = "custom_" + 简短下划线命名（如 custom_coding_language / custom_theme_color），
  value = 具体取值（自由文本）。绝不要因为找不到精确键就丢弃一条真实偏好。
- 喜好/身份/物品类也可落在 habit.likes / habit.dislikes / habit.occupation / habit.possessions。

Do NOT invent these unless clearly said:
- workflow.preview_before_execute — ONLY if user says 预览/先看计划/直接做不用确认/不用问
- Do not add random workflow keys

Pure one-shot with no policy (return []):
- "把这份文件打印出来"

Output STRICT JSON array only. No markdown fences.
Each item fields: category, key (short, no prefix), value, description, scope, scene_id, reason
bool/enum values must be exact schema tokens (true/false, excel, zip, pdf, date_prefix, bash, python, ...).
"""

FEW_SHOT_BLOCK = """
Few-shot (copy this extraction style; output JSON array only):

Input: 帮我把这个文件从桌面挪到 D 盘项目文件夹
Output:
[{"category":"habit","key":"download_path","value":"D:/Downloads","description":"文件落到 D 盘","scope":"scene","scene_id":"os_operation","reason":"D盘路径习惯"},{"category":"habit","key":"file_naming_rule","value":"project_prefix","description":"按项目组织文件","scope":"scene","scene_id":"os_operation","reason":"项目文件夹"}]

Input: 帮我把这周的日报整理成表格发给我
Output:
[{"category":"tool_selection","key":"data_processing_tool","value":"excel","description":"表格整理用 Excel","scope":"scene","scene_id":"office","reason":"整理成表格"}]

Input: 把桌面上所有 .tmp 文件删了，直接删不用问我
Output:
[{"category":"security","key":"confirm_before_rm","value":"false","description":"删除不确认","scope":"scene","scene_id":"os_operation","reason":"不用问"},{"category":"workflow","key":"preview_before_execute","value":"false","description":"不预览直接执行","scope":"scene","scene_id":"os_operation","reason":"直接删"}]

Input: 帮我搜一下采购合同，上周改过的那个
Output:
[{"category":"tool_selection","key":"file_search_tool","value":"everything","description":"文件搜索","scope":"scene","scene_id":"office","reason":"搜合同"}]

Input: 用英文给我出一份这个 Excel 的分析报告
Output:
[{"category":"style","key":"reply_language","value":"en-US","description":"英文","scope":"scene","scene_id":"office","reason":"用英文"},{"category":"style","key":"report_format","value":"docx","description":"报告格式","scope":"scene","scene_id":"office","reason":"分析报告"}]

Input: 把 downloads 按类型分类放到不同文件夹
Output:
[{"category":"habit","key":"file_organize_rule","value":"by_type","description":"按类型整理","scope":"scene","scene_id":"os_operation","reason":"按类型分类"},{"category":"workflow","key":"backup_before_modify","value":"true","description":"整理前备份","scope":"scene","scene_id":"os_operation","reason":"批量移动文件宜备份"}]

Input: 把这个目录打个压缩包，密码设一下
Output:
[{"category":"habit","key":"archive_format","value":"zip","description":"压缩 zip","scope":"scene","scene_id":"os_operation","reason":"压缩包"},{"category":"security","key":"confirm_before_highrisk","value":"true","description":"设密需确认","scope":"scene","scene_id":"os_operation","reason":"密码"}]

Input: 帮我把这个 Word 转换成 PDF 发给客户
Output:
[{"category":"style","key":"report_format","value":"pdf","description":"输出 PDF","scope":"scene","scene_id":"office","reason":"转 PDF"},{"category":"workflow","key":"auto_complete_steps","value":"true","description":"自动补全发送客户","scope":"scene","scene_id":"office","reason":"发给客户"}]

Input: 帮我批量重命名这些图片，按日期顺序
Output:
[{"category":"habit","key":"file_naming_rule","value":"date_prefix","description":"按日期命名","scope":"scene","scene_id":"os_operation","reason":"按日期顺序"},{"category":"tool_selection","key":"batch_operation_tool","value":"bash","description":"批量操作用 bash","scope":"scene","scene_id":"os_operation","reason":"批量重命名"}]

Input: 把这份合同里的日期都改成 2026-08-01 格式
Output:
[{"category":"tool_selection","key":"text_processing_tool","value":"python","description":"文本批量处理","scope":"scene","scene_id":"office","reason":"合同批量改日期"},{"category":"workflow","key":"backup_before_modify","value":"true","description":"改合同前备份","scope":"scene","scene_id":"office","reason":"合同修改"}]

Input: 把这份文件打印出来
Output:
[]
"""

EXTRACTION_USER_PROMPT = """Allowed preference keys:
{allowed_keys}
{few_shot}
User_id: {user_id}
Hint scene_id: {scene_id}
Source: {source}

Input texts:
{texts}

Extract ALL matching preferences as a JSON array (no markdown):
"""


def build_extraction_messages(
    texts: list[str],
    user_id: str,
    scene_id: str | None,
    source: str,
) -> list[dict[str, str]]:
    user = EXTRACTION_USER_PROMPT.format(
        allowed_keys=ALLOWED_KEYS_TEXT,
        few_shot=FEW_SHOT_BLOCK,
        user_id=user_id,
        scene_id=scene_id or "general",
        source=source,
        texts="\n---\n".join(texts),
    )
    return [
        {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# 开放域偏好抽取（竞赛用）
# 上面的封闭键提示词只覆盖 OS Agent 的固定键表，面对「用 TypeScript」「只要代码不要
# 解释」「回答简洁」这类开放域自由偏好会返回空。这里新增一个开放域抽取头，让模型把
# 用户表达的持久偏好以自由文本 condition→action 抽出来，落库为 custom_ 动态键，再由
# 评测侧的语义判卷（LLM-judge）判断是否覆盖金标准，而不是硬贴 OS 键抬分。
# ---------------------------------------------------------------------------
OPEN_DOMAIN_SYSTEM_PROMPT = """You are an EXHAUSTIVE user-preference extractor.

Given a user message, list EVERY requirement, instruction, style rule, constraint, or implied
preference about HOW the assistant should do the task or respond — now and in the future. This is
OPEN-DOMAIN and NOT limited to a fixed key table. Be exhaustive: prefer extracting a borderline
item over missing a real preference.

Make sure to cover, including TASK-SPECIFIC and IMPLICIT ones:
- response style / tone (concise, information-dense, no explanation, no filler)
- output format / structure (single line, numbered segments, only code, JSON, table)
- language (answer / write / respond in English or Chinese)
- tools or technologies implied by the request (use Java, use TypeScript, use Midjourney
  /imagine structure, use Node.js/Express/MongoDB)
- explicit constraints and negative rules (avoid the word "description", do not use colons)
- domain role / persona (act as a MERN developer, act as a brand designer)
- quantity / count requirements (produce 4 prompts)
- any standing instruction that should apply to future similar requests

Output a STRICT JSON array only (no markdown, no prose). Each item:
{
  "category": one of ["habit","style","security","tool_selection","workflow","communication","general"],
  "key": short snake_case label (e.g. coding_language / output_format / response_tone),
  "value": concise value or target (e.g. "Java" / "single line" / "concise"),
  "condition": when the preference applies (e.g. "code examples" / "image prompts"; "" if general),
  "description": a faithful, self-contained natural-language sentence of the preference,
                 keep the user's own wording as much as possible,
  "confidence": number in [0,1]
}

Examples:
1) "Could you provide a detailed Java implementation of the LinkedList superclass ..."
   -> [{"category":"habit","key":"coding_language","value":"Java","condition":"code examples",
        "description":"Use Java for code examples.","confidence":0.9}]
2) "... generate four single-line Midjourney prompts in English ... follow the usual /imagine
   prompt structure with numbered segments ... do not use the words 'description' or ':' ..."
   -> [{"category":"style","key":"prompt_language","value":"English","condition":"image prompts",
        "description":"Write prompts in English.","confidence":1.0},
       {"category":"style","key":"prompt_count","value":"4","condition":"image prompts",
        "description":"Create 4 varied prompts per concept.","confidence":0.9},
       {"category":"tool_selection","key":"prompt_structure","value":"/imagine numbered segments",
        "condition":"image prompts",
        "description":"Follow the /imagine prompt structure with numbered segments.","confidence":0.9}]

If the message is a pure factual question or pure one-shot request with NO requirement at all
about how to respond or do the task, output [].
"""

OPEN_DOMAIN_USER_PROMPT = """User message:
{texts}

Extract all durable preferences as a JSON array (no markdown):
"""


def build_open_domain_messages(texts: list[str]) -> list[dict[str, str]]:
    """开放域抽取消息：不注入封闭键表，鼓励自由文本 condition→action。"""
    user = OPEN_DOMAIN_USER_PROMPT.format(texts="\n---\n".join(texts))
    return [
        {"role": "system", "content": OPEN_DOMAIN_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
