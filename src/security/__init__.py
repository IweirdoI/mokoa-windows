"""Security module for MOKOA.

This module currently provides rule-based sensitive information filtering and a
local sensitive-value index. Precise NL forgetting and context maintenance are
planned but are not implemented yet.

"""

from src.security.service import (
    SecurityAndForgetService,
    SensitivePattern,
    SensitiveIndex,
    redact_sensitive_text,
)

__all__ = [
    "SecurityAndForgetService",
    "SensitivePattern",
    "SensitiveIndex",
    "redact_sensitive_text",
]
