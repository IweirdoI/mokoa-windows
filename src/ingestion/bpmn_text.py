"""Extract readable process labels from BPMN XML.

Diagram files store task names in ``name`` attributes, often split across
newlines. The XML prologue and BPMNDI geometry must not be treated as
document text for embedding or retrieval.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

_WHITESPACE = re.compile(r"\s+")
_EXERCISE = re.compile(r"(?:exercise|ex|练习|题)[-_]?\d+", re.I)
_TASK_TAGS = {
    "task",
    "usertask",
    "servicetask",
    "manualtask",
    "scripttask",
    "sendtask",
    "receivetask",
    "businessruletask",
    "callactivity",
    "subprocess",
}
_SKIP_TAGS = {
    "bounds",
    "waypoint",
    "bpmnshape",
    "bpmnedge",
    "bpmnlabel",
    "bpmnplane",
    "bpmndiagram",
    "incoming",
    "outgoing",
    "extensionelements",
}

# 通用/泛化任务名：大量流程图首个任务都叫这些，区分度低。近重复图（同一练习
# 被多人重画）靠它们无法互相区分，检索时优先跳过，挑更具辨识度的任务名。
_GENERIC_TASK_NAMES = {
    # 英文
    "start", "end", "begin", "begin activity", "finish", "complete", "completed",
    "review", "submit", "approve", "reject", "check", "verify", "validate",
    "send", "receive", "notify", "create", "update", "delete", "process",
    "task", "activity", "step", "do", "done", "ok", "next", "start event",
    "end event", "send email", "send e-mail", "log", "record", "register",
    # 中文
    "开始", "结束", "开始活动", "完成", "审核", "提交", "审批", "检查", "验证",
    "发送", "接收", "通知", "创建", "更新", "删除", "处理", "任务", "活动",
    "步骤", "下一步", "记录", "登记", "发送邮件", "通知客户",
}


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _parse_bpmn(xml_text: str) -> "ET.Element":
    """解析 BPMN XML，禁用外部实体与 DTD，防止 XXE / 十亿笑实体炸弹造成 DoS。

    Python 3.13+ 直接用 ``XMLParser(forbid_dtd/forbid_entities)``；旧版本无该参数，
    回退为剥离 DOCTYPE（实体炸弹依赖 DOCTYPE 内定义），使自定义实体引用因未定义
    而在解析时抛 ``ParseError``（由调用方捕获返回空）。深层嵌套触发的 ``RecursionError``
    由调用方捕获，避免穿透成 API 500。
    """
    import sys

    if sys.version_info >= (3, 13):
        parser = ET.XMLParser(forbid_dtd=True, forbid_entities=True)
        return ET.fromstring(xml_text, parser=parser)
    # 旧版本回退：去掉 DOCTYPE 子集，自定义实体定义随之消失，引用即报错被捕获。
    cleaned = re.sub(r"<!DOCTYPE[^>]*>", "", xml_text, count=1)
    return ET.fromstring(cleaned)


def _label(raw: str) -> str:
    return _WHITESPACE.sub(" ", (raw or "").strip())


def bpmn_filename(name: str | None) -> str:
    if not name:
        return ""
    return Path(str(name)).name


def bpmn_exercise_id(filename: str | None) -> str:
    """Keep ``ex00`` / ``exercise-04`` so near-duplicate diagrams stay distinct."""
    match = _EXERCISE.search(bpmn_filename(filename))
    return match.group(0) if match else ""


def extract_bpmn_labels(
    xml_text: str,
    *,
    max_chars: int = 1500,
    filename: str | None = None,
) -> dict:
    """Return unique process labels and a compact document body.

    ``text`` is empty when the XML cannot be parsed or has no ``name`` values.
    Callers must not fall back to the XML prefix to manufacture a retrieval hit.
    When ``filename`` is given, the body keeps ``file=`` / ``exercise=`` so
    same-task diagrams from different exercises do not collide.
    """
    file_name = bpmn_filename(filename)
    exercise = bpmn_exercise_id(file_name)
    empty = {
        "text": "",
        "names": [],
        "tasks": [],
        "parse_ok": False,
        "filename": file_name,
        "exercise": exercise,
        "content_truncated": False,
    }
    names: list[str] = []
    tasks: list[str] = []
    seen: set[str] = set()
    try:
        root = _parse_bpmn(xml_text)
    except (ET.ParseError, RecursionError):
        return empty

    for el in root.iter():
        tag = _local(el.tag)
        if tag.lower() in _SKIP_TAGS:
            continue
        label = _label(el.attrib.get("name") or "")
        if not label or label in seen:
            continue
        seen.add(label)
        names.append(label)
        if tag.lower() in _TASK_TAGS:
            tasks.append(label)

    text = ""
    if names:
        head = "BPMN process steps: "
        extras: list[str] = []
        if file_name:
            extras.append(f"file={file_name}")
        if exercise:
            extras.append(f"exercise={exercise}")
        if extras:
            head += "(" + " ".join(extras) + ") "
        text = head + "; ".join(names)
    return {
        "text": text[:max_chars],
        "names": names,
        "tasks": tasks,
        "parse_ok": True,
        "filename": file_name,
        "exercise": exercise,
        "content_truncated": len(text) > max_chars,
    }


def bpmn_task_query(extracted: dict, *, max_chars: int = 180) -> str:
    """Discriminating task/process names plus filename/exercise, never XML prefix.

    近重复流程图（同一练习被多人重画，首个任务名常相同）会互相挤占 top-5。
    因此优先挑选区分度高的「非通用」任务名（跳过 Start/Review/End 之类），
    并把 file+exercise 前置，使查询更像本图、更少被同题他图截胡。
    """
    def norm(s: str) -> str:
        return _WHITESPACE.sub(" ", (s or "").strip()).lower()

    tasks = list(dict.fromkeys(extracted.get("tasks") or []))
    names = list(dict.fromkeys(extracted.get("names") or extracted.get("labels") or []))

    # 非通用任务名排前面：保序去重后按是否通用排序。
    ordered_tasks = sorted(tasks, key=lambda x: (norm(x) in _GENERIC_TASK_NAMES, 0))

    picked: list[str] = []
    seen: set[str] = set()

    def add(item: str) -> None:
        key = norm(item)
        if not item or key in seen:
            return
        seen.add(key)
        picked.append(item)

    # 1) 先取非通用任务名（最多 4 个）
    for item in ordered_tasks:
        if norm(item) in _GENERIC_TASK_NAMES:
            continue
        if len(picked) >= 4:
            break
        add(item)
    # 2) 不足则补非通用 process/其他名
    if len(picked) < 4:
        for item in names:
            if norm(item) in _GENERIC_TASK_NAMES:
                continue
            if len(picked) >= 4:
                break
            add(item)
    # 3) 仍不足才用通用名兜底
    if len(picked) < 4:
        for item in ordered_tasks:
            if len(picked) >= 4:
                break
            add(item)
    if len(picked) < 4:
        for item in names:
            if len(picked) >= 4:
                break
            add(item)

    bits: list[str] = []
    file_name = bpmn_filename(extracted.get("filename") or extracted.get("file") or "")
    if file_name:
        bits.append(file_name)
    exercise = str(extracted.get("exercise") or bpmn_exercise_id(file_name) or "")
    if exercise and exercise not in bits:
        bits.append(exercise)
    bits.extend(picked[:4])
    if not bits:
        return ""
    return " ".join(bits)[:max_chars]
