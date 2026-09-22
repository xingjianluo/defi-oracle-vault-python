"""多来源价格摄入与聚合。

判定流程（每一步都记录可解释依据）：

1. 不在资产来源配置里的报价 -> UNCONFIGURED；
2. 配置来源没有报价 / price 为 None -> MISSING；
3. 报价非正数或非有限数字 -> NON_POSITIVE；
4. 报价年龄超过 max_price_age_seconds（含异常的未来时间戳）-> STALE；
5. 通过初筛的候选，以其中位数为基准，相对偏离超过
   outlier_deviation -> OUTLIER（仅有 2 个候选且互相偏离超阈时，
   无法仲裁，两个都拒绝）；
6. 存活来源数 < min_valid_sources -> 资产明确不可用（P005），
   绝不回退到陈旧/默认价格；
7. 否则按来源权重做加权平均，量化到 price_precision。
"""

import logging
import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .errors import AssetNotConfiguredError
from .models import (
    AggregationResult,
    AssetConfig,
    PriceQuote,
    RejectReason,
    SourceRejection,
)

logger = logging.getLogger("vault.price")

# 允许的报价时钟偏差（秒）：超过此时长的"未来报价"同样视为异常。
_FUTURE_TOLERANCE = 1.0


def _to_decimal(value: object) -> Decimal | None:
    """尽力转 Decimal；NaN/Inf/不可解析返回 None。"""
    return safe_decimal(value)


def safe_decimal(value: object) -> Decimal | None:
    """把外部输入安全转换为有限 Decimal；任何不可解析/NaN/Inf 都返回 None。

    供门面/HTTP 层在进入聚合前调用，避免裸 ``InvalidOperation`` 穿透到
    业务错误处理之外；None 随后会按"非正数/缺失"路径被明确拒绝。
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        dec = value
    else:
        try:
            dec = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return None
    if not dec.is_finite():
        return None
    return dec


def _median(values: list[Decimal]) -> Decimal:
    """有序列表的中位数；偶数个取中间两项的算术平均。"""
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


class PriceAggregator:
    def __init__(self, config: AssetConfig, clock=None):
        self.config = config
        self._clock = clock or time.time
        # 最近一次聚合结果，便于外部（如熔断模块）读取。
        self.last_result: AggregationResult | None = None

    # ------------------------------------------------------------ 摄入 -----

    def ingest(self, quotes: list[PriceQuote]) -> AggregationResult:
        cfg = self.config
        now = self._clock()
        configured = {s.name: s for s in cfg.sources}
        rejections: list[SourceRejection] = []

        logger.info("[%s] 聚合开始 now=%.3f, 收到报价 %d 条", cfg.symbol, now, len(quotes))

        # 同一来源多条报价时，只取 observed_at 最新的一条。
        latest: dict[str, PriceQuote] = {}
        for q in quotes:
            if q.source not in configured:
                rejections.append(
                    SourceRejection(
                        source=q.source,
                        reason=RejectReason.UNCONFIGURED,
                        detail="该来源不在资产来源配置中",
                        raw_price=q.price,
                    )
                )
                logger.info(
                    "[%s] 来源=%s 拒绝(UNCONFIGURED): 未配置的来源, raw=%s",
                    cfg.symbol, q.source, q.price,
                )
                continue
            old = latest.get(q.source)
            if old is None or q.observed_at > old.observed_at:
                latest[q.source] = q

        candidates: dict[str, Decimal] = {}

        # 步骤 2~4：逐来源校验。
        for source_name in configured:
            q = latest.get(source_name)
            if q is None or q.price is None:
                rejections.append(
                    SourceRejection(
                        source=source_name,
                        reason=RejectReason.MISSING,
                        detail="配置来源缺失报价",
                        raw_price=None,
                    )
                )
                logger.info("[%s] 来源=%s 拒绝(MISSING): 无报价", cfg.symbol, source_name)
                continue

            price = _to_decimal(q.price)
            if price is None:
                rejections.append(
                    SourceRejection(
                        source=source_name,
                        reason=RejectReason.NON_POSITIVE,
                        detail="报价无法解析为有限数字",
                        raw_price=q.price,
                    )
                )
                logger.info(
                    "[%s] 来源=%s 拒绝(NON_POSITIVE): 不可解析 raw=%r",
                    cfg.symbol, source_name, q.price,
                )
                continue
            if price <= 0:
                rejections.append(
                    SourceRejection(
                        source=source_name,
                        reason=RejectReason.NON_POSITIVE,
                        detail=f"报价必须为正数, got {price}",
                        raw_price=price,
                    )
                )
                logger.info(
                    "[%s] 来源=%s 拒绝(NON_POSITIVE): price=%s",
                    cfg.symbol, source_name, price,
                )
                continue

            age = now - q.timestamp
            if age > cfg.max_price_age_seconds:
                rejections.append(
                    SourceRejection(
                        source=source_name,
                        reason=RejectReason.STALE,
                        detail=(
                            f"报价过期 age={age:.3f}s > max_age={cfg.max_price_age_seconds:.3f}s"
                        ),
                        raw_price=price,
                    )
                )
                logger.info(
                    "[%s] 来源=%s 拒绝(STALE): price=%s age=%.3fs max=%.3fs",
                    cfg.symbol, source_name, price, age, cfg.max_price_age_seconds,
                )
                continue
            if age < -_FUTURE_TOLERANCE:
                rejections.append(
                    SourceRejection(
                        source=source_name,
                        reason=RejectReason.STALE,
                        detail=f"报价时间戳异常(未来) age={age:.3f}s",
                        raw_price=price,
                    )
                )
                logger.info(
                    "[%s] 来源=%s 拒绝(STALE): 未来时间戳 age=%.3fs",
                    cfg.symbol, source_name, age,
                )
                continue

            logger.info(
                "[%s] 来源=%s 初筛通过: price=%s age=%.3fs",
                cfg.symbol, source_name, price, age,
            )
            candidates[source_name] = price

        # 步骤 5：离群检测。
        accepted = self._reject_outliers(candidates, rejections)

        # 步骤 6：有效来源不足 -> 不可用。
        result: AggregationResult
        if len(accepted) < cfg.min_valid_sources:
            codes = sorted({r.reason.value for r in rejections})
            result = AggregationResult(
                symbol=cfg.symbol,
                usable=False,
                price=None,
                median=None,
                accepted=accepted,
                rejections=tuple(rejections),
                observed_at=now,
                failure_code="P005",
            )
            logger.warning(
                "[%s] 判定不可用 P005: 有效来源 %d < 要求 %d, 拒绝原因集合=%s",
                cfg.symbol, len(accepted), cfg.min_valid_sources, codes,
            )
            self.last_result = result
            return result

        # 步骤 7：加权平均。
        weights = {s.name: s.weight for s in cfg.sources}
        total_w = sum(weights[name] for name in accepted)
        raw_price = sum(accepted[name] * weights[name] for name in accepted) / total_w
        median = _median(list(accepted.values()))
        price = raw_price.quantize(cfg.price_precision, rounding=ROUND_HALF_UP)

        result = AggregationResult(
            symbol=cfg.symbol,
            usable=True,
            price=price,
            median=median.quantize(cfg.price_precision, rounding=ROUND_HALF_UP),
            accepted=accepted,
            rejections=tuple(rejections),
            observed_at=now,
        )
        logger.info(
            "[%s] 判定可用: 接受来源=%s 加权均价(舍入前)=%s 聚合价=%s 中位数=%s",
            cfg.symbol, sorted(accepted), raw_price, price, result.median,
        )
        self.last_result = result
        return result

    # ---------------------------------------------------------- 离群检测 -----

    def _reject_outliers(
        self,
        candidates: dict[str, Decimal],
        rejections: list[SourceRejection],
    ) -> dict[str, Decimal]:
        cfg = self.config
        if len(candidates) <= 1:
            return dict(candidates)

        values = list(candidates.values())
        base = _median(values)
        if base == 0:
            return dict(candidates)

        accepted: dict[str, Decimal] = {}
        for name, value in candidates.items():
            deviation = abs(value - base) / base
            if deviation > cfg.outlier_deviation:
                rejections.append(
                    SourceRejection(
                        source=name,
                        reason=RejectReason.OUTLIER,
                        detail=(
                            f"相对中位数偏离 {deviation} > 阈值 {cfg.outlier_deviation}; "
                            f"value={value}, median={base}"
                        ),
                        raw_price=value,
                    )
                )
                logger.warning(
                    "[%s] 来源=%s 拒绝(OUTLIER): price=%s median=%s deviation=%s threshold=%s",
                    cfg.symbol, name, value, base, deviation, cfg.outlier_deviation,
                )
            else:
                accepted[name] = value
        return accepted

    # ------------------------------------------------------------ 查询 -----

    def require_asset(self, symbol: str) -> None:
        """门面用：资产不在配置中即报错。"""
        if symbol != self.config.symbol:
            raise AssetNotConfiguredError(symbol)
