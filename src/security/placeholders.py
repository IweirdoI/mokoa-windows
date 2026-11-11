"""脱敏占位符模式共享常量。

安全脱敏层（src/security/service.py）把敏感内容替换为两类占位符：
  - 常规占位符  ``<HASH_xxxxxxxx>``        （8 位 hex，尖括号包裹，用于正文脱敏）
  - LLM 占位符   ``<_HASH_xxxxxxxxxxxxxxxx_>``（16 位 hex，下划线包裹，用于 LLM 上下文脱敏）

分类器（src/core/classifier.py）需要识别这些占位符以便跳过它们，
因此把模式集中在此处统一定义，避免两处正则漂移、保证语义一致。
"""

from __future__ import annotations

import re

# 同时匹配常规占位符 <HASH_8hex> 与 LLM 占位符 <_HASH_16hex_>（大小写不敏感）。
PLACEHOLDER_PATTERN = r"(?:<HASH_[0-9a-f]{8}>|<_HASH_[0-9a-f]{16}_>)"
PLACEHOLDER_RE = re.compile(PLACEHOLDER_PATTERN, re.I)

# 仅 LLM 上下文占位符（16 位 hex），用于需要区分两类占位符的场景。
LLM_PLACEHOLDER_PATTERN = r"<_HASH_[0-9a-f]{16}_>"
LLM_PLACEHOLDER_RE = re.compile(LLM_PLACEHOLDER_PATTERN)
