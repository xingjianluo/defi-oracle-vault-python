"""业务错误类型。

每一种失败语义对应一个独立的异常类，调用方可以按类型区分：
价格源拒绝原因、估值失败、账本操作失败、熔断失败各自独立。
"""

from __future__ import annotations


class VaultError(Exception):
    """所有本库业务错误的基类。"""


# ---- 价格源 / 聚合相关 ----


class PriceFeedError(VaultError):
    """价格摄入与聚合相关错误的基类。"""


class QuoteRejected(PriceFeedError):
    """单条报价被拒绝参与聚合的基类，``source`` 标识来源。"""

    def __init__(self, source: str, reason: str) -> None:
        self.source = source
        self.reason = reason
        super().__init__(f"报价来源 {source!r} 被拒绝: {reason}")


class QuoteMissing(QuoteRejected):
    """该来源在当前批次中缺失（根本没有报价）。"""


class QuoteExpired(QuoteRejected):
    """报价时间戳早于允许的新鲜度窗口。"""

    def __init__(self, source: str, age_seconds: float, max_age_seconds: float) -> None:
        self.age_seconds = age_seconds
        self.max_age_seconds = max_age_seconds
        super().__init__(
            source,
            f"报价已过期(age={age_seconds:.3f}s > max_age={max_age_seconds:.3f}s)",
        )


class QuoteNonPositive(QuoteRejected):
    """报价为非正数（<=0），不可用于估值。"""

    def __init__(self, source: str, value: str) -> None:
        self.value = value
        super().__init__(source, f"报价非正数(value={value})")


class QuoteOutlier(QuoteRejected):
    """报价明显偏离其余有效来源（离群）。"""

    def __init__(self, source: str, value: str, median: str, deviation: str, threshold: str) -> None:
        self.value = value
        self.median = median
        self.deviation = deviation
        self.threshold = threshold
        super().__init__(
            source,
            f"离群(value={value}, median={median}, deviation={deviation} > {threshold})",
        )


class InsufficientValidSources(PriceFeedError):
    """有效来源数量不足，资产被明确判定为不可用（绝不回退陈旧/默认价格）。"""

    def __init__(self, asset: str, valid: int, required: int, rejected: list[QuoteRejected]) -> None:
        self.asset = asset
        self.valid = valid
        self.required = required
        self.rejected = rejected
        reasons = "; ".join(f"{r.source}:{r.reason}" for r in rejected)
        super().__init__(
            f"资产 {asset!r} 有效来源不足: {valid}/{required}; 拒绝明细=[{reasons}]"
        )


class NoPriceObservation(PriceFeedError):
    """该资产从未摄入过任何报价。"""

    def __init__(self, asset: str) -> None:
        self.asset = asset
        super().__init__(f"资产 {asset!r} 从无价格观测")


# ---- 估值相关 ----


class ValuationError(VaultError):
    """估值失败基类。"""


class AssetUnavailable(ValuationError):
    """资产当前不可用（价格无效/有效来源不足），不允许依据陈旧价格操作。"""

    def __init__(self, asset: str, detail: str) -> None:
        self.asset = asset
        self.detail = detail
        super().__init__(f"资产 {asset!r} 当前不可用: {detail}")


# ---- 账本 / 份额操作相关 ----


class LedgerError(VaultError):
    """账本操作失败基类。"""


class UnknownAsset(LedgerError):
    """账本中未注册该资产。"""

    def __init__(self, asset: str) -> None:
        self.asset = asset
        super().__init__(f"未知资产 {asset!r}")


class InvalidAmount(LedgerError):
    """金额/份额非法（非正数、精度非法等）。"""


class InsufficientShares(LedgerError):
    """用户份额余额不足以完成提取。"""

    def __init__(self, user: str, requested: str, available: str) -> None:
        self.user = user
        self.requested = requested
        self.available = available
        super().__init__(f"用户 {user!r} 份额不足: 请求={requested}, 可用={available}")


# ---- 熔断 / 风控相关 ----


class CircuitBreakerError(VaultError):
    """熔断相关失败基类。"""


class CircuitOpen(CircuitBreakerError):
    """熔断生效中，新增敞口（存入）被暂停。"""

    def __init__(self, asset: str, trigger: str) -> None:
        self.asset = asset
        self.trigger = trigger
        super().__init__(f"资产 {asset!r} 熔断生效中, 暂停新增敞口: {trigger}")


class UnauthorizedRecovery(CircuitBreakerError):
    """恢复操作者不具备所需权限。"""

    def __init__(self, operator: str) -> None:
        self.operator = operator
        super().__init__(f"操作者 {operator!r} 无权执行熔断恢复")


class RecoveryConditionsNotMet(CircuitBreakerError):
    """熔断恢复的明确条件尚未满足。"""

    def __init__(self, asset: str, reasons: list[str]) -> None:
        self.asset = asset
        self.reasons = reasons
        super().__init__(f"资产 {asset!r} 恢复条件不满足: {'; '.join(reasons)}")


# ---- 历史回看相关 ----


class HistoryError(VaultError):
    """历史回看查询相关失败基类。"""


class InvalidHistoryQuery(HistoryError):
    """历史查询入参非法（时间点非有限数等），请求方必须能与业务不可用区分。"""


class HistoryOutOfRetention(HistoryError):
    """查询时点早于该资产的历史留存下界，数据已按留存策略淘汰，无法回答。

    与 NoPriceObservation（该时点当时确实还没有任何报价）是两种结论：
    前者是"查不到/已淘汰"，后者是"留存可覆盖且当时确无报价"。
    """

    def __init__(self, asset: str, queried_ts: float, horizon_ts: float | None) -> None:
        self.asset = asset
        self.queried_ts = queried_ts
        self.horizon_ts = horizon_ts
        super().__init__(
            f"资产 {asset!r} 的历史查询时点 {queried_ts} 已超出留存范围 "
            f"(留存下界={horizon_ts})，该时点结论不可复现"
        )


class NoPositionAtTime(HistoryError):
    """用户在该历史时点之前没有发生过任何份额变动（当时尚无持仓）。

    与"资产当时不可用"严格区分：无持仓是用户维度事实，不依赖价格。
    """

    def __init__(self, user: str, asset: str, ts: float) -> None:
        self.user = user
        self.asset = asset
        self.ts = ts
        super().__init__(f"用户 {user!r} 在 {ts} 时尚无资产 {asset!r} 的持仓记录")
