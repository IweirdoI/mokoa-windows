"""BPMN 解析安全边界回归测试。

防止畸形 XML 造成服务级问题：
- 十亿笑实体炸弹 / XXE：剥离 DOCTYPE 后自定义实体未定义，解析应返回空而非展开吞内存。
- 深层嵌套：不应抛出未捕获异常（防御性 RecursionError 兜底）。
- 正常 BPMN：抽取任务名的功能不受影响。
"""

import pytest

from src.ingestion.bpmn_text import extract_bpmn_labels


def test_billion_laughs_returns_empty():
    bomb = (
        '<?xml version="1.0"?>'
        "<!DOCTYPE x[ "
        '<!ENTITY a "a">'
        '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
        '<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">'
        "]><root>&c;</root>"
    )
    r = extract_bpmn_labels(bomb)
    assert r["parse_ok"] is False


def test_deeply_nested_does_not_crash():
    deep = "<a>" * 40000 + "</a>" * 40000
    r = extract_bpmn_labels(deep)
    assert isinstance(r, dict)
    assert "parse_ok" in r


def test_normal_bpmn_works():
    ok = (
        '<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL">'
        "<bpmn:process>"
        '<bpmn:task name="提交报销"/>'
        '<bpmn:task name="审批报销"/>'
        "</bpmn:process></bpmn:definitions>"
    )
    r = extract_bpmn_labels(ok)
    assert r["parse_ok"] is True
    assert "提交报销" in r["tasks"]
