"""核心数据结构（不可变值对象 + 可变状态快照）。"""

from __future__ import annotations

from dataclasses import dataclass
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
class SourceRejection:
    """一条来源被拒绝的可解释记录。"""

    source: str
    reason_code: str  # missing / expired / non_positive / outlier / unconfigured
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


@dataclass(frozen=True)
class HistoricalAggregationResult:
    """过去某一时点的聚合判定（只读回放结果）。

    - queried_ts: 查询的历史时点；只使用 ts <= queried_ts 的报价（不泄漏未来）
    - oldest_retained_ts: 该资产当前留存报价的最早时间（留存下界，审计用）；
      None 表示从未有任何报价
    - used_sources / rejected: 当时口径下的使用与拒绝明细，语义与实时聚合一致
    """

    asset: str
    queried_ts: float
    price: Decimal
    used_sources: tuple[str, ...]
    rejected: tuple[SourceRejection, ...]
    oldest_retained_ts: float | None


@dataclass(frozen=True)
class LedgerEvent:
    """一次份额变动的仅追加事件（带全局序号，同时点也有确定顺序）。"""

    seq: int
    ts: float
    kind: str  # deposit / withdraw
    user: str
    asset: str
    shares_delta: Decimal  # 带符号：存入为正、提取为负
    balance_after: Decimal


@dataclass(frozen=True)
class HistoricalPosition:
    """用户在某历史时点的份额余额与按当时聚合价计算的持仓市值。"""

    user: str
    asset: str
    queried_ts: float
    shares: Decimal
    price: Decimal
    value: Decimal
    used_sources: tuple[str, ...]
    rejected: tuple[SourceRejection, ...]


@dataclass(frozen=True)
class PositionReconciliation:
    """历史份额与当前份额的对账结果。

    恒等式：current_shares == historical_shares + net_delta，
    其中 net_delta 为 (historical_ts, now] 区间内该用户全部份额变动之和；
    events 给出每一笔可解释的净变化。
    """

    user: str
    asset: str
    historical_ts: float
    current_ts: float
    historical_shares: Decimal
    current_shares: Decimal
    net_delta: Decimal
    matches: bool
    events: tuple[LedgerEvent, ...]
