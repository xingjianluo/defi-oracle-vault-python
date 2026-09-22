"""所有可区分的失败类型。

每条业务失败都带有稳定的错误码 code，调用方（含测试与 HTTP 层）
可据此区分不同失败原因，而不是依赖字符串匹配。
"""


class VaultError(Exception):
    """业务错误基类。"""

    code: str = "V000"


# ---------------------------------------------------------------- 价格聚合 --


class PriceError(VaultError):
    """价格侧失败基类。"""


class AssetNotConfiguredError(PriceError):
    code = "P000"

    def __init__(self, symbol: str):
        self.symbol = symbol
        super().__init__(f"资产 {symbol} 未配置")


class PriceStaleError(PriceError):
    """来源报价超过最大年龄。"""

    code = "P001"

    def __init__(self, source: str, age_seconds: float, max_age: float):
        self.source = source
        self.age_seconds = age_seconds
        self.max_age = max_age
        super().__init__(
            f"来源 {source} 报价过期: age={age_seconds:.3f}s > max_age={max_age:.3f}s"
        )


class PriceMissingError(PriceError):
    """配置的来源根本没有报价。"""

    code = "P002"

    def __init__(self, source: str):
        self.source = source
        super().__init__(f"来源 {source} 缺失报价")


class PriceNonPositiveError(PriceError):
    """报价为非正数或无法解析。"""

    code = "P003"

    def __init__(self, source: str, value: object):
        self.source = source
        self.value = value
        super().__init__(f"来源 {source} 报价非法(非正数): {value!r}")


class PriceOutlierError(PriceError):
    """报价显著偏离其余有效来源。"""

    code = "P004"

    def __init__(self, source: str, value: str, median: str, deviation: str, threshold: str):
        self.source = source
        self.value = value
        self.median = median
        self.deviation = deviation
        self.threshold = threshold
        super().__init__(
            f"来源 {source} 报价 {value} 离群: 相对中位数 {median} 偏离 {deviation} "
            f"> 阈值 {threshold}"
        )


class InsufficientSourcesError(PriceError):
    """通过校验的有效来源数量不足，资产判定为不可用。"""

    code = "P005"

    def __init__(self, asset: str, valid: int, required: int, rejections: list[str]):
        self.asset = asset
        self.valid = valid
        self.required = required
        self.rejections = rejections
        super().__init__(
            f"资产 {asset} 有效来源不足: {valid}/{required}; 拒绝原因: {', '.join(rejections)}"
        )


class PriceUnavailableError(PriceError):
    """操作时刻没有足够新鲜的有效估值（可能从未聚合成功或已陈旧）。"""

    code = "P006"

    def __init__(self, asset: str, detail: str):
        self.asset = asset
        self.detail = detail
        super().__init__(f"资产 {asset} 当前不可用于估值: {detail}")


# ---------------------------------------------------------------- 账本/份额 --


class LedgerError(VaultError):
    """账本侧失败基类。"""


class InsufficientSharesError(LedgerError):
    code = "L001"

    def __init__(self, user: str, asset: str, requested: str, available: str):
        self.user = user
        self.asset = asset
        self.requested = requested
        self.available = available
        super().__init__(
            f"用户 {user} 资产 {asset} 份额不足: 请求 {requested}, 可用 {available}"
        )


class InvalidAmountError(LedgerError):
    code = "L002"

    def __init__(self, detail: str):
        super().__init__(f"非法金额/份额: {detail}")


# ---------------------------------------------------------------- 熔断 -----


class CircuitBreakerOpenError(VaultError):
    """熔断生效，拒绝新增敞口。"""

    code = "C001"

    def __init__(self, asset: str, reasons: list[str]):
        self.asset = asset
        self.reasons = reasons
        super().__init__(f"资产 {asset} 熔断中，暂停新增敞口; 触发依据: {'; '.join(reasons)}")


class CircuitBreakerNotTrippedError(VaultError):
    """熔断未触发，无需恢复。"""

    code = "C002"

    def __init__(self, asset: str):
        self.asset = asset
        super().__init__(f"资产 {asset} 熔断未触发，无需恢复")


class RecoveryConditionsNotMetError(VaultError):
    """恢复条件未满足。"""

    code = "C003"

    def __init__(self, unmet: list[str]):
        self.unmet = unmet
        super().__init__(f"恢复条件不满足: {'; '.join(unmet)}")


class UnauthorizedRecoveryError(VaultError):
    """操作者无恢复权限。"""

    code = "C004"

    def __init__(self, operator: str):
        self.operator = operator
        super().__init__(f"操作者 {operator} 无熔断恢复权限")
