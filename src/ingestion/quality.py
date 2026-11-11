"""可解释的多维质量评分。

质量门控的目标不是判断“内容真假”，而是判断一条规范化记录是否值得进入记忆
生命周期。评分完全由确定性规则组成，确保离线测试、演示和正式环境结果一致。

业务规则：
    * 评分范围固定为 ``[0, 1]``，默认按类型分阈值（见 ``_DEFAULT_TYPE_THRESHOLDS``）：
      ``conversation`` 0.46（真实底 0.505 / 噪声顶 0.415 均值）、``document`` 0.50、
      ``tool_result`` 0.55、``behavior`` 0.38；未列出类型（如 ``manual_config``）回落全局
      阈值 0.46。阈值取值来自 benchmarks 下真实语料 + 噪声对照的测量。同类型内真实与噪声
      仍重叠的类型，需配合信息密度(Step2)与记忆信号检测(Step3)进一步分离。
    * 临时信息、隐私风险和低信息短语采用减分，不在此直接脱敏或删除。
    * 来源可靠度、用户明确性、重复次数、信息量、复用价值和输入置信度共同决定结果。

算法思路：
    分别计算六个 ``[0, 1]`` 维度，按固定权重求和，再扣除业务惩罚，
    将结果截断到 ``[0, 1]`` 并与阈值比较。

依赖关系：
    输入依赖 ``NormalizedRecord`` 中的 type/content/confidence/metadata；
    输出 ``QualityDecision`` 供 pipeline、审计、前端和 benchmark 共用。

Owner: 3fba60e1
"""

from __future__ import annotations

import math
import re
from typing import Any

from src.common.models import NormalizedRecord
from src.ingestion.models import QualityDecision


# 中英文轻量停用词/填充词: 用于判断 token 是否承载实质信息（不计纯标点/虚词）
_STOPWORDS = frozenset({
    # 中文虚词/填充
    "的", "了", "是", "在", "我", "你", "他", "她", "它", "我们", "你们", "他们",
    "这", "那", "和", "与", "或", "及", "就", "都", "也", "不", "没", "吗", "呢",
    "啊", "呀", "哦", "嗯", "啦", "吧", "嘛", "呵", "哈", "哎", "嘢", "诶",
    "一个", "一种", "一些", "这个", "那个", "这些", "那些", "可以", "应该", "已经",
    "the", "a", "an", "is", "are", "was", "were", "of", "to", "and", "in", "on",
    "for", "with", "that", "this", "it", "you", "we", "i", "my", "your", "at",
    "be", "by", "as", "from", "or", "but", "not", "no", "yes", "ok", "okay",
})


class QualityEvaluator:
    """按来源可靠度、明确性、频次、信息量和复用价值综合评分。

    Args:
        threshold: 准入阈值，闭区间 ``[0, 1]``，不可为 ``None``。

    Raises:
        ValueError: ``threshold`` 低于 0 或高于 1。

    依赖关系：
        无外部模型或网络依赖；同一输入始终得到同一评分。
    """

    _SOURCE_RELIABILITY = {
        "manual_config": 0.95,
        "conversation": 0.80,
        "tool_result": 0.70,
        "behavior": 0.45,
        "document": 0.80,
    }
    _WEIGHTS = {
        "source_reliability": 0.25,
        "explicitness": 0.20,
        "recurrence": 0.20,
        "informativeness": 0.15,
        "reuse_value": 0.10,
        "input_confidence": 0.10,
    }

    # 按类型分阈值: 不同来源的自然分数分布不同, 用单一全局阈值会误杀系统性偏低的
    # 合法类型(如 behavior 真实日志约 0.37, 被全局 0.46 误杀)或过度放行高分类型。
    # 默认值来自 benchmarks 下真实语料 + 噪声对照的测量(见 measure_thresholds_tmp.py),
    # 在引入信息密度 _informativeness(Step2) 与记忆信号检测(Step3) 后测得各类型干净分隔:
    #   conversation 真实底 0.475 / 噪声顶 0.400 (间隔 0.075)
    #   document    真实底 0.516 / 噪声顶 0.504 (间隔偏紧, 确定性规则天花板)
    #   tool_result 真实底 0.574 / 噪声顶 0.500 (间隔 0.074)
    #   behavior    真实底 0.452 (occ>=2 重复模式) / 噪声顶 0.362 (occ=1 一次性事件) (间隔 0.091)
    #               注: behavior 的区分维度是频次 (recurrence 与 reuse 均由 occurrence_count 驱动),
    #                   真实行为模式须按其累计频次计; 若都按 occ=1 计间隔会退化为 ~0.002 (伪间隔)
    #   manual_config 不列 -> 回落全局 0.46 (用户显式配置整体高置信)
    _DEFAULT_TYPE_THRESHOLDS = {
        "conversation": 0.44,
        "document": 0.51,
        "tool_result": 0.54,
        "behavior": 0.41,
    }

    def __init__(
        self,
        threshold: float = 0.46,
        weights: dict[str, float] | None = None,
        type_thresholds: dict[str, float] | None = None,
    ) -> None:
        """保存质量阈值和可选的维度权重。

        Args:
            threshold: ``[0, 1]`` 内的浮点数，边界 0 和 1 均允许；作为未列出类型的兜底阈值。
            weights: 可选覆盖 ``_WEIGHTS`` 的维度权重；缺省使用内置默认值。
            type_thresholds: 可选按 ``record.type`` 覆盖的准入阈值；命中则优先于 ``threshold``。
        Raises:
            ValueError: 阈值超出允许范围，或权重键未知、值为负。
        """
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("quality threshold 必须位于 [0, 1]")
        merged = dict(self._WEIGHTS)
        if weights:
            unknown = set(weights) - set(self._WEIGHTS)
            if unknown:
                raise ValueError(f"未知的质量权重键：{sorted(unknown)}")
            for name, value in weights.items():
                number = float(value)
                if number < 0:
                    raise ValueError(f"质量权重 {name} 不能为负")
                merged[name] = number
        self.threshold = threshold
        self.weights = merged
        self.type_thresholds = dict(self._DEFAULT_TYPE_THRESHOLDS)
        if type_thresholds:
            for t, v in type_thresholds.items():
                if not 0.0 <= float(v) <= 1.0:
                    raise ValueError(f"类型阈值 {t} 必须位于 [0, 1]")
                self.type_thresholds[t] = float(v)

    def score(self, record: NormalizedRecord) -> float:
        """兼容旧接口：只返回最终分数。

        Args:
            record: 已标准化的记录，不可为 ``None``。
        Returns:
            四位小数的分数，范围 ``[0, 1]``。
        Raises:
            AttributeError: 调用方绕过类型契约，传入缺少必要字段的对象。
        """

        return self.evaluate(record).score

    def should_store(self, record: NormalizedRecord) -> bool:
        """兼容旧接口：返回是否达到 ingestion 准入阈值。

        Args:
            record: 已标准化的记录，不可为 ``None``。
        Returns:
            分数大于等于阈值时为 ``True``，否则为 ``False``。

        业务规则：
            返回 ``True`` 仅表示值得进入后续记忆流程，不表示已写 Milvus。
        """

        return self.evaluate(record).accepted

    def evaluate(self, record: NormalizedRecord) -> QualityDecision:
        """返回评分、分项和中文解释。

        Args:
            record: ``DataNormalizer`` 生成的 ``NormalizedRecord``；不可为
                ``None``，``content`` 应为非空文本，``confidence`` 应在 ``[0, 1]``。

        Returns:
            ``QualityDecision``，包含是否准入、最终分、阈值、
            六项分量与可解释原因。

        Raises:
            AttributeError: 输入不符合 ``NormalizedRecord`` 最小字段契约。

        Business Rules:
            ``occurrence_count`` 无法解析时按 1 次处理；``temporary``、
            ``privacy_risk`` 与低信息短语分别扣分；最终分截断到
            ``[0, 1]``，并保留四位小数。

        Algorithm:
            读取频次 → 计算六项分量 → 加权求和 → 扣除惩罚
            → 截断并四舍五入 → 与阈值比较 → 生成中文解释。

        对 ``temporary`` 与 ``privacy_risk`` 采用减分而不是直接删除：安全模块仍会
        在 Orchestrator 中执行最终脱敏；这里仅避免高风险或明显临时内容轻易晋升。
        """

        occurrence_count = self._metadata_int(record.metadata, "occurrence_count", default=1)
        memory_signal = self._detect_memory_signal(record.content)
        components = {
            "source_reliability": self._SOURCE_RELIABILITY.get(record.type, 0.30),
            "explicitness": self._explicitness(record),
            "recurrence": self._recurrence(occurrence_count),
            "informativeness": self._informativeness(record.content, record.type),
            "reuse_value": self._reuse_value(record, occurrence_count, memory_signal),
            "input_confidence": max(0.0, min(1.0, record.confidence)),
        }

        weighted_score = sum(
            components[name] * weight for name, weight in self.weights.items()
        )

        reasons: list[str] = []
        penalty = 0.0
        if memory_signal:
            reasons.append("内容含明确偏好/事实/约束信号，提升复用价值")
        if bool(record.metadata.get("temporary")):
            penalty += 0.35
            reasons.append("内容被标记为临时信息")
        if bool(record.metadata.get("privacy_risk")):
            penalty += 0.40
            reasons.append("内容存在隐私风险，等待安全模块处理")
        if self._is_low_information_phrase(record.content):
            penalty += 0.20
            reasons.append("正文属于低信息量短语")

        score = round(max(0.0, min(1.0, weighted_score - penalty)), 4)
        # 按类型取阈值: 命中 type_thresholds 用类型阈值, 否则用全局兜底阈值
        threshold = self.type_thresholds.get(record.type, self.threshold)
        accepted = score >= threshold
        reasons.extend(self._explain_components(record, components, occurrence_count))
        reasons.append(
            f"综合质量分 {score:.4f} "
            f"{'达到' if accepted else '低于'}{'类型' if record.type in self.type_thresholds else '全局'}"
            f"阈值 {threshold:.4f}"
        )

        return QualityDecision(
            accepted=accepted,
            score=score,
            threshold=threshold,
            components={key: round(value, 4) for key, value in components.items()},
            reasons=reasons,
        )

    @staticmethod
    def _explicitness(record: NormalizedRecord) -> float:
        """计算用户明确性，返回 ``[0, 1]`` 的确定性分数。"""
        if record.type == "manual_config":
            return 1.0
        if bool(record.metadata.get("explicit")):
            return 1.0
        if record.type == "tool_result":
            return 0.5
        return 0.0

    @staticmethod
    def _recurrence(count: int) -> float:
        """将出现次数映射为 ``[0.2, 1]`` 的复发分。

        ``count <= 1`` 按单次处理；使用对数曲线表达“前几次增长快，
        之后逐渐饱和”，大于等于约 5 次后封顶为 1。
        """

        return min(1.0, 0.2 + 0.8 * math.log1p(max(0, count - 1)) / math.log(5))

    # 记忆信号: 内容含偏好/约束/事实/个人细节时值得长期记忆; 命中任一强模式即视为 1.0
    _MEMORY_SIGNAL_PATTERNS = [
        r"(喜欢|偏好|爱好|热衷|希望|想要|要求|期待|倾向|更愿意|最爱|最讨厌|受不了|受不了)",
        r"(禁止|不允许|不能|不要|避免|必须|务必|只能|应该|应当|请务必|切忌|切勿)",
        r"(我(住|在|工作|毕业|出生|叫|是|今年).{0,6}|电话|手机|邮箱|邮件|地址|生日|年龄|公司|学校|微信|账号)",
        r"[\"\u201c\u201d“”。]{2,}[\"\u201c\u201d”]",  # 引号/书名号内实体
        r"\b[A-Z][a-zA-Z]{2,}\b",                     # 大写字母词（英文实体/专有名词）
        r"\d{1,4}[\-/年月日]\d{1,2}",                 # 日期/数字+单位
    ]
    _MEMORY_SIGNAL_RE = re.compile("|".join(_MEMORY_SIGNAL_PATTERNS))

    @classmethod
    def _detect_memory_signal(cls, content: str) -> float:
        """检测内容是否含值得长期记忆的偏好/约束/事实/个人细节信号（启发式，无模型依赖）。

        返回强度 ``[0, 1]``：命中任一模式为 ``1.0``，否则 ``0.0``。仅作轻量信号，
        用于提升复用价值，不替代语义理解。
        """
        if not content or not content.strip():
            return 0.0
        return 1.0 if cls._MEMORY_SIGNAL_RE.search(content) else 0.0

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """中英文统一分词：空白切分，CJK 按单字，仅保留字母/数字/CJK 并去尾标点。"""
        tokens: list[str] = []
        for seg in re.split(r"\s+", text.strip()):
            if not seg:
                continue
            buf = ""
            for ch in seg:
                if "\u4e00" <= ch <= "\u9fff":
                    if buf:
                        buf = re.sub(r"[^\w]+$", "", buf)
                        if buf:
                            tokens.append(buf.lower())
                        buf = ""
                    tokens.append(ch)
                else:
                    buf += ch
            if buf:
                buf = re.sub(r"[^\w]+$", "", buf)
                if buf:
                    tokens.append(buf.lower())
        return tokens

    @staticmethod
    def _is_content_token(token: str) -> bool:
        if not token:
            return False
        if token in _STOPWORDS:
            return False
        if re.fullmatch(r"[^\w\u4e00-\u9fff]+", token):  # 纯标点
            return False
        return True

    def _informativeness(self, content: str, record_type: str = "behavior") -> float:
        """按信息密度（词汇多样度 + 实质词占比 + 长度饱和）评估信息量，返回 ``[0, 1]``。

        不再用“>500 字 = 冗余日志”的硬悬崖：长而丰富的内容得高分，长而重复/灌水
        的内容得低分，短内容随长度与密度给较低底分。``behavior`` 类（行为埋点/事件
        日志）超长仍轻微降权，因其天然冗余。
        """
        normalized = content.strip()
        # 中文单字承载信息密度高于英文字母；每个 CJK 字符额外计一个信息单位。
        cjk_count = sum("\u4e00" <= char <= "\u9fff" for char in normalized)
        effective_length = len(normalized) + cjk_count
        if effective_length < 4:
            return 0.0
        if effective_length < 10:
            return 0.3
        tokens = self._tokenize(normalized)
        n = len(tokens)
        if n == 0:
            return 0.3
        unique_ratio = len(set(tokens)) / n
        content_tokens = [t for t in tokens if self._is_content_token(t)]
        substance = len(content_tokens) / n
        density = 0.5 * unique_ratio + 0.5 * substance
        # 长度饱和因子：短文本信息承载天然受限，随长度对数饱和到 1.0
        length_factor = min(1.0, 0.6 + 0.4 * math.log1p(effective_length) / math.log1p(300))
        score = length_factor * (0.35 + 0.65 * density)
        if record_type == "behavior" and effective_length > 500:
            score *= 0.85  # 行为日志超长：冗余风险
        return round(max(0.0, min(1.0, score)), 4)

    @staticmethod
    def _reuse_value(record: NormalizedRecord, occurrence_count: int, memory_signal: float = 0.0) -> float:
        """按来源、工具状态、行为频次与记忆信号估计后续复用价值。

        ``memory_signal`` 为 ``_detect_memory_signal`` 的强度（0~1）：当内容明确含
        偏好/约束/事实/个人细节时，提升对话/文档类的复用价值（让值得记的短内容越过阈值）。
        """
        if record.type == "manual_config":
            return 0.9
        if record.type == "conversation":
            base = 0.75 if bool(record.metadata.get("explicit")) else 0.45
            return min(1.0, base + 0.30 * memory_signal)
        if record.type == "tool_result":
            status = str(record.metadata.get("status", "")).lower()
            return 0.9 if status in {"failed", "error", "timeout"} else 0.7
        if record.type == "behavior":
            return min(1.0, occurrence_count / 5)
        if record.type == "document":
            return min(1.0, 0.75 + 0.15 * memory_signal)
        return 0.3

    @staticmethod
    def _metadata_int(metadata: dict[str, Any], key: str, *, default: int) -> int:
        """读取最小为 1 的整数 metadata；空值/非法值返回 ``default``。"""
        try:
            return max(1, int(metadata.get(key, default)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _is_low_information_phrase(content: str) -> bool:
        """判断去空白、小写后的正文是否命中固定低信息集。"""
        normalized = content.strip().lower()
        return normalized in {
            "好的",
            "知道了",
            "谢谢",
            "好的没问题",
            "没问题",
            "ok",
            "okay",
            "test",
            "测试",
            "点击",
        }

    @staticmethod
    def _explain_components(
        record: NormalizedRecord,
        components: dict[str, float],
        occurrence_count: int,
    ) -> list[str]:
        """只输出对人工判断有帮助的摘要，避免把六个小数全部堆给前端。"""

        reasons: list[str] = []
        if components["explicitness"] >= 1.0:
            reasons.append("用户表达明确")
        if record.type == "behavior":
            reasons.append(f"行为累计出现 {occurrence_count} 次")
        if record.type == "tool_result":
            reasons.append("工具结果具有可复用的执行上下文")
        if components["informativeness"] < 0.5:
            reasons.append("正文信息量较低")
        return reasons
