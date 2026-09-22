"""数据模型与配置项。

全部金额使用 Decimal 定点运算；价格也用 Decimal，避免浮点误差
在"守恒"校验里凭空产生/吞掉资产。
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

ZERO = Decimal(0)


# ---------------------------------------------------------------- 配置 -----


@dataclass(frozen=True)
class SourceConfig:
    """单个价格来源配置。"""

    name: str
    weight: Decimal = Decimal(1)


@dataclass(frozen=True)
class AssetConfig:
    """单个资产的估值与风控配置。

    - min_valid_sources: 参与聚合所需的最少有效来源数
    - max_price_age_seconds: 报价最大年龄，超过即判定过期
    - outlier_deviation: 离群阈值（相对中位数的偏离比例，如 0.05=5%）
    - volatility_threshold: 观察窗口内允许的最大相对波动
    - observation_window_seconds: 波动观察窗口
    - unavailable_grace_seconds: 价格持续不可用多久后触发熔断
    - recovery_required_valid_observations: 恢复所需连续有效估值次数
    """

    symbol: str
    sources: tuple[SourceConfig, ...]
    min_valid_sources: int = 2
    max_price_age_seconds: float = 60.0
    outlier_deviation: Decimal = Decimal("0.05")
    volatility_threshold: Decimal = Decimal("0.10")
    observation_window_seconds: float = 300.0
    unavailable_grace_seconds: float = 120.0
    recovery_required_valid_observations: int = 3
    price_precision: Decimal = Decimal("0.00000001")  # 8 位小数
    share_precision: Decimal = Decimal("0.00000001")
    cash_precision: Decimal = Decimal("0.00000001")


@dataclass(frozen=True)
class VaultConfig:
    """全局配置。"""

    assets: dict[str, AssetConfig]
    recovery_operators: frozenset[str] = frozenset({"risk_admin"})
    initial_price: Decimal = Decimal(0)


# ---------------------------------------------------------------- 报价 -----


@dataclass(frozen=True)
class PriceQuote:
    """一个来源对一个资产的一次报价。"""

    source: str
    price: Decimal | None  # None 表示来源缺失/无法给出
    timestamp: float  # 报价时间（epoch 秒）
    observed_at: float  # 系统接收时间（epoch 秒）


class RejectReason(str, Enum):
    MISSING = "missing"          # 来源缺失
    STALE = "stale"              # 过期
    NON_POSITIVE = "non_positive"  # 非正数
    OUTLIER = "outlier"          # 离群
    UNCONFIGURED = "unconfigured_source"  # 不在资产来源配置中


@dataclass(frozen=True)
class SourceRejection:
    source: str
    reason: RejectReason
    detail: str
    raw_price: Decimal | None = None


@dataclass(frozen=True)
class AggregationResult:
    """一次聚合的完整、可解释结论。"""

    symbol: str
    usable: bool
    price: Decimal | None
    median: Decimal | None
    accepted: dict[str, Decimal]  # source -> price
    rejections: tuple[SourceRejection, ...]
    observed_at: float
    failure_code: str | None = None  # 不可用时的错误码（如 P005）


# ---------------------------------------------------------------- 熔断状态 --


class BreakerState(str, Enum):
    CLOSED = "closed"  # 正常
    OPEN = "open"      # 熔断，暂停新增敞口


@dataclass(frozen=True)
class BreakerTrigger:
    """熔断触发依据（不可变记录）。"""

    kind: str  # "volatility" | "prolonged_unavailable"
    at: float
    detail: str


@dataclass(frozen=True)
class RecoveryRecord:
    """受控恢复记录。"""

    at: float
    operator: str
    previous_trigger: BreakerTrigger
    consecutive_valid: int
    detail: str


# ---------------------------------------------------------------- 账本事件 --


@dataclass(frozen=True)
class LedgerEntry:
    """一次存取记录。"""

    tx_id: str
    user: str
    symbol: str
    kind: str  # "deposit" | "withdraw"
    shares: Decimal
    price: Decimal
    cash_delta: Decimal
    dust_delta: Decimal
    at: float


@dataclass
class AccountSnapshot:
    user: str
    symbol: str
    shares: Decimal = ZERO


@dataclass(frozen=True)
class OperationReceipt:
    """一次成功存取的回执（含估值依据）。"""

    entry: LedgerEntry
    price: Decimal
    breaker_state: BreakerState


@dataclass(frozen=True)
class AssetStatus:
    """资产运行时状态快照（可解释）。"""

    symbol: str
    breaker: BreakerState
    usable: bool
    last_price: Decimal | None
    last_observed_at: float | None
    triggers: tuple[BreakerTrigger, ...]
    total_shares: Decimal
    net_cash: Decimal
