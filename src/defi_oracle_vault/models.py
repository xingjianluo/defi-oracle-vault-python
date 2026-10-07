"""核心数据结构（不可变值对象 + 可变状态快照）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum


@dataclass(frozen=True)
class Quote:
    """单个价格源对某资产的一条报价。

    value 使用 Decimal 表示单位资产的价格（计价货币/资产单位）。
    ts 为报价观测时间（Unix 秒，单调时钟由调用方保证）。
    """

    asset: str
    source: str
    value: Decimal
    ts: float


@dataclass(frozen=True)
class HistoryRetention:
    """历史报价保留策略（有界，防止无限占内存）。

    按 (资产, 来源) 序列独立执行，摄入时滚动清理：
    - max_age_seconds: 只保留与该序列最新报价时间差不超过该值的条目
      （边界含等号：newest_ts - entry.ts <= max_age_seconds 即保留）；
      None 表示不按年龄清理。
    - max_entries_per_source: 每个序列最多保留的条数（超出时丢弃最旧的）；
      None 表示不按条数清理。
    两者同时配置时同时生效（任一条件触发即清理）。
    注意：被清理不等于"当时没有报价"——每个序列的最早观测时间
    会被永久记住，用于区分两种结论。
    """

    max_age_seconds: float | None = 3600.0
    max_entries_per_source: int | None = 10000


@dataclass(frozen=True)
class ShareEvent:
    """一次份额变动事件（账本回放用，追加式不可变）。

    seq 为全局单调序号，用于相同 ts 下的确定性排序（先发生先回放）。
    delta 为正表示存入、为负表示提取。
    """

    seq: int
    ts: float
    user: str
    delta: Decimal
    kind: str  # deposit / deposit_value / withdraw


@dataclass(frozen=True)
class SourceRejection:
    """一条来源被拒绝的可解释记录。"""

    source: str
    reason_code: str  # missing / pruned / expired / non_positive / outlier / unconfigured
    detail: str


@dataclass(frozen=True)
class AggregationResult:
    """一次聚合的完整判定依据（含被拒来源，便于审计与日志）。"""

    asset: str
    price: Decimal
    used_sources: tuple[str, ...]
    rejected: tuple[SourceRejection, ...]
    computed_ts: float


@dataclass(frozen=True)
class AssetConfig:
    """资产的价格源与风控配置。

    - sources: 允许参与聚合的来源集合
    - min_valid_sources: 聚合所需最少有效来源数
    - max_age_seconds: 报价最大允许年龄（过期即拒）
    - outlier_deviation: 离群阈值，相对中位数的偏离比例(如 0.05 表示 5%)
    - volatility_window_seconds: 熔断波动观察窗口长度
    - volatility_threshold: 窗口内 (max-min)/min 波动比例阈值
    - unavailable_grace_seconds: 价格持续不可用多久后触发熔断
    """

    asset: str
    sources: tuple[str, ...]
    min_valid_sources: int = 1
    max_age_seconds: float = 60.0
    outlier_deviation: Decimal = Decimal("0.05")
    volatility_window_seconds: float = 300.0
    volatility_threshold: Decimal = Decimal("0.10")
    unavailable_grace_seconds: float = 120.0


class CircuitState(str, Enum):
    CLOSED = "CLOSED"  # 正常
    OPEN = "OPEN"  # 熔断中：暂停新增敞口


@dataclass(frozen=True)
class TriggerEvidence:
    """熔断触发依据（可解释、可审计）。"""

    code: str  # volatility / prolonged_unavailable
    detail: str
    opened_ts: float


@dataclass
class CircuitStatus:
    """某资产的熔断状态快照。"""

    asset: str
    state: CircuitState = CircuitState.CLOSED
    evidence: TriggerEvidence | None = None
    last_good_price: Decimal | None = None
    last_good_ts: float | None = None


@dataclass(frozen=True)
class OperationReceipt:
    """一次存取操作的凭证（含当时估值依据）。"""

    kind: str  # deposit / withdraw
    user: str
    asset: str
    shares: Decimal
    price: Decimal
    ts: float
    balance_after: Decimal
    total_shares_after: Decimal
