"""Context perception engine for dynamic message handling.

Owner: 2abac5a6
Ref: Context-aware compression, Priority-based analysis, Streaming support
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol

# 压缩策略阈值（量纲见 analyze()：token_pressure / volatility / info_density 均取值 0~1）。
#   token_pressure: 上下文窗口占用率 = token_usage / context_window
#   volatility: 语义活跃度 = 最近轮次角色切换频率（越高话题跳得越快）
#   info_density: 信息密度 = 事实类消息占比
_RESTRUCTURE_PRESSURE = 0.9  # 窗口占用率超过该值 -> 深度重构（restructure）
_SUMMARY_PRESSURE = 0.6      # 窗口占用率超过该值 -> 轻量摘要（summary）
_SUMMARY_VOLATILITY = 0.7    # 话题切换频率超过该值 -> 轻量摘要（summary）


class ContextBlock:
    """上下文分块，用于流式处理。"""
    
    def __init__(self, messages: list[dict[str, Any]], priority: str) -> None:
        self.messages = messages
        self.priority = priority
        self.is_compressed = False
        self.compressed_content: str | None = None
        
    @property
    def is_cached(self) -> bool:
        """检查是否已缓存（长期不进入压缩候选）。"""
        return self.priority in ("system", "user_prompt", "long_term")
        
    @property
    def is_compressible(self) -> bool:
        """检查是否允许压缩。"""
        return not self.is_cached and not self.is_compressed


class KeywordRule:
    """单条关键词规则，用于快速过滤。"""
    
    def __init__(
        self,
        name: str,
        keywords: list[str],
        action: str = "compress",  # "compress" 或 "discard"
    ) -> None:
        self.name = name
        self.keywords = [k.lower() for k in keywords]
        self.action = action


class ContextAnalyzer:
    """对当前上下文进行感知分析，评估压缩需求并建议处理策略。
    
    采用分层架构：
    - 系统提示词/用户提示词：放在最前面，永不压缩
    - 长期记忆：缓存命中率高，不参与压缩队列
    - 短期对话：按轮数分块，支持流式压缩
    """
    
    def __init__(
        self,
        summary_model: str | None = None,
        keywords_path: str = "configs/keywords.yaml",
        block_size: int = 5,
    ) -> None:
        # 摘要模型默认从顶层 llm 段读取（llm.model），未配置时回退本地模型名
        if summary_model is None:
            from src.common.config import get_llm_config

            summary_model = str(get_llm_config().get("model", "local_qwen_7b"))
        self._summary_model = summary_model
        self._keywords_path = keywords_path
        self._block_size = block_size
        self._keywords: list[KeywordRule] = []
        self._load_keywords()
        self._last_check_time = 0.0
        self._check_interval = 10.0  # 每 10 秒检查一次
        
    def _load_keywords(self) -> None:
        """加载关键词规则。

        配置文件存在时读取；不存在或解析失败时回退到内置默认规则，
        保证 analyzer 在任何环境都能获得可用规则。
        """
        keywords_path = Path(self._keywords_path)
        if not keywords_path.exists():
            # 配置文件缺失：直接使用内置默认规则
            self._keywords = self._get_default_rules()
            return
        try:
            with open(keywords_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                for rule_data in data.get("rules", []):
                    self._keywords.append(
                        KeywordRule(
                            name=rule_data["name"],
                            keywords=rule_data.get("keywords", []),
                            action=rule_data.get("action", "compress"),
                        )
                    )
        except Exception:
            # 如果加载失败，使用默认规则
            self._keywords = self._get_default_rules()
    
    def _get_default_rules(self) -> list[KeywordRule]:
        """获取默认关键词规则。"""
        return [
            KeywordRule(
                name="tool_output",
                keywords=["stdout", "stderr", "exit_code", "output"],
                action="compress",
            ),
            KeywordRule(
                name="repetitive_greeting",
                keywords=["你好", "hello", "hi", "你好啊"],
                action="compress",
            ),
            KeywordRule(
                name="confirmation",
                keywords=["好的", "明白", "了解", "好的呢"],
                action="compress",
            ),
        ]
    
    def analyze(
        self, 
        messages: list[dict[str, Any]], 
        token_usage: int = 0,
        context_window: int = 128000,
    ) -> dict[str, Any]:
        """分析当前上下文状态，评估压缩需求。
        
        Args:
            messages: 当前对话消息列表
            token_usage: 当前 Token 使用量
            context_window: 上下文窗口大小
            
        Returns:
            包含分析结果的字典
        """
        
        # 1. 基础状态评估
        token_pressure = token_usage / context_window if context_window > 0 else 0
        conversation_length = len(messages)
        
        # 2. 语义活跃度评估
        volatility = self._measure_topic_changes(messages)
        
        # 3. 信息密度评估
        info_density = self._calculate_information_density(messages)
        
        # 4. 根据优先级选择处理策略
        strategy = self._select_strategy(
            token_pressure=token_pressure,
            volatility=volatility,
            info_density=info_density,
        )
        
        return {
            "token_pressure": token_pressure,
            "volatility": volatility,
            "info_density": info_density,
            "strategy": strategy,
            "needs_summary": strategy == "summary",
            "needs_pruning": strategy == "prune",
            "deep_restructure_needed": strategy == "restructure",
        }
    
    def _measure_topic_changes(self, messages: list[dict[str, Any]]) -> float:
        """评估语义活跃度（话题变化频率）。"""
        if len(messages) <= 2:
            return 0.0
        # 简化实现：计算最近 5 轮对话的角色变化频率
        recent = messages[-5:] if len(messages) >= 5 else messages
        role_changes = 0
        for i in range(1, len(recent)):
            if recent[i].get("role") != recent[i-1].get("role"):
                role_changes += 1
        return role_changes / (len(recent) - 1) if len(recent) > 1 else 0.0
    
    def _calculate_information_density(self, messages: list[dict[str, Any]]) -> float:
        """计算信息密度（事实 vs 闲聊的比例）。"""
        if not messages:
            return 0.5
        
        info_content = 0
        total = len(messages)
        
        for msg in messages:
            content = msg.get("content", "")
            # 检查是否包含关键信息特征
            if (any(kw in content.lower() for kw in ["code", "error", "file", "path", "config"]) or
                msg.get("role") in ["assistant", "system"] or
                "tool" in str(msg).lower()):
                info_content += 1
        
        return info_content / total if total > 0 else 0.5
    
    def _select_strategy(self, **kwargs: Any) -> str:
        """根据优先级选择处理策略。

        三个输入量纲均为 0~1（见 analyze()）：token_pressure 窗口占用率、
        volatility 话题切换频率、info_density 信息密度。阈值见模块级常量。
        """
        token_pressure = kwargs.get("token_pressure", 0)
        volatility = kwargs.get("volatility", 0)
        info_density = kwargs.get("info_density", 0)

        # 高压力（窗口即将撑爆）-> 深度重构
        if token_pressure > _RESTRUCTURE_PRESSURE:
            return "restructure"
        # 中等压力或高活跃度（话题跳得快）-> 轻量摘要
        elif token_pressure > _SUMMARY_PRESSURE or volatility > _SUMMARY_VOLATILITY:
            return "summary"
        # 低压力 -> 轻量过滤
        else:
            return "prune"
    
    def stream_compress(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """流式压缩：在用户对话进行时就逐步压缩。
        
        Args:
            messages: 当前对话消息列表
            
        Returns:
            压缩后的消息列表
        """
        compressed = []
        current_block = []
        
        for msg in messages:
            current_block.append(msg)
            
            # 每 block_size 轮进行一次压缩
            if len(current_block) >= self._block_size:
                # 压缩当前块
                compressed_block = self._compress_block(current_block)
                compressed.append(compressed_block)
                current_block = []
        
        # 处理剩余的轮次
        if current_block:
            compressed_block = self._compress_block(current_block)
            compressed.append(compressed_block)
            
        return compressed
    
    def _compress_block(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """压缩单个对话块。
        
        采用分层策略：
        1. 关键词快速过滤（最快，优先使用）
        2. 模型摘要（较慢，但更智能）
        3. 语义检索兜底（当模型不可用时）
        """
        # 尝试关键词过滤
        filtered_msgs = self._keyword_filter(messages)
        
        # 如果过滤后仍有消息，使用模型摘要
        if filtered_msgs:
            summary = self._generate_summary(filtered_msgs)
        else:
            summary = "早期对话已压缩处理，无关键信息保留。"
        
        return {
            "role": "system",
            "content": f"【上下文压缩】压缩自 {len(messages)} 轮对话：{summary}",
            "compressed_from": len(messages),
        }
    
    def _keyword_filter(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """使用关键词规则过滤消息。"""
        filtered = []
        for msg in messages:
            content = msg.get("content", "")
            is_matched = False
            for rule in self._keywords:
                if any(kw in content.lower() for kw in rule.keywords):
                    is_matched = True
                    if rule.action == "discard":
                        break  # 跳过此消息
                    else:
                        filtered.append(msg)  # 保留此消息
                    break
            if not is_matched:
                filtered.append(msg)
        return filtered
    
    def _generate_summary(self, messages: list[dict[str, Any]]) -> str:
        """生成消息摘要。
        
        尝试使用模型，如果不可用则使用简单关键词提取。
        """
        try:
            # 尝试使用模型生成摘要
            summary = self._llm_summary(messages)
            return summary
        except Exception:
            # 模型不可用，使用关键词提取
            return self._keyword_summary(messages)
    
    def _llm_summary(self, messages: list[dict[str, Any]]) -> str:
        """使用 LLM 生成摘要。"""
        # 这里应该调用实际的 LLM 服务
        # 由于是示例实现，使用简单模板
        user_msgs = [m for m in messages if m.get("role") == "user"]
        assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
        
        user_actions = [m.get("content", "")[:50] for m in user_msgs[:3]]
        assistant_actions = [m.get("content", "")[:50] for m in assistant_msgs[:3]]
        
        summary = f"用户进行了{len(user_msgs)}次操作，助手提供了{len(assistant_msgs)}次回复。"
        if user_actions:
            summary += f" 用户主要操作：{'; '.join(user_actions)}。"
        return summary
    
    def _keyword_summary(self, messages: list[dict[str, Any]]) -> str:
        """使用关键词提取生成摘要。"""
        all_content = " ".join([m.get("content", "") for m in messages])
        # 提取前 100 个字符作为摘要
        return f"对话内容摘要：{all_content[:100]}..."
    
    def get_compression_stats(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """获取压缩统计信息。"""
        total_tokens = sum(len(m.get("content", "")) for m in messages)
        cached_blocks = sum(1 for m in messages if m.get("priority", "") in ("system", "user_prompt", "long_term"))
        
        return {
            "total_messages": len(messages),
            "total_tokens": total_tokens,
            "cached_blocks": cached_blocks,
            "compressible_blocks": len(messages) - cached_blocks,
        }
