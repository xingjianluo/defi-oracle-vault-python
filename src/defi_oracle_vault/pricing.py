"""多价格源摄入与聚合。

拒绝原因彼此可区分：
- missing:       配置了但当前没有任何报价
- expired:       报价年龄超过 max_age_seconds
- non_positive:  报价 <= 0
- outlier:       相对候选中位数偏离超过 outlier_deviation
- unconfigured:  报价来自该资产未配置的来源，不参与聚合

有效来源不足时抛 InsufficientValidSources，绝不回退陈旧/默认价格。
"""

from __future__ import annotations

import logging
import threading
from decimal import Decimal, localcontext

from .errors import (
    InsufficientValidSources,
    NoPriceObservation,
    QuoteExpired,
    QuoteMissing,
    QuoteNonPositive,
    QuoteOutlier,
    QuoteRejected,
)
from .models import AggregationResult, AssetConfig, Quote, SourceRejection

logger = logging.getLogger(__name__)

_REASON_CODE = {
    QuoteMissing: "missing",
    QuoteExpired: "expired",
    QuoteNonPositive: "non_positive",
    QuoteOutlier: "outlier",
}


class _UnconfiguredSource(QuoteRejected):
    """内部：未配置来源的拒绝（reason_code 映射为 unconfigured）。"""


_REASON_CODE[_UnconfiguredSource] = "unconfigured"


class PriceBook:
    """线程安全的本地价格簿：保存每个资产每个来源最新一条报价。"""

    def __init__(self) -> None:
        self._quotes: dict[tuple[str, str], Quote] = {}
        self._assets: set[str] = set()
        self._lock = threading.RLock()

    def ingest(self, quote: Quote) -> None:
        """摄入（覆盖）一条报价。"""
        with self._lock:
            self._quotes[(quote.asset, quote.source)] = quote
            self._assets.add(quote.asset)
            logger.info(
                "ingest asset=%s source=%s value=%s ts=%s",
                quote.asset,
                quote.source,
                quote.value,
                quote.ts,
            )

    def latest(self, asset: str, source: str) -> Quote | None:
        """读取某资产某来源的最新报价（无则 None）。"""
        with self._lock:
            return self._quotes.get((asset, source))

    def snapshot(self, asset: str, sources: tuple[str, ...]) -> dict[str, Quote | None]:
        """原子地读取多来源最新报价。"""
        with self._lock:
            return {s: self._quotes.get((asset, s)) for s in sources}

    def sources_for(self, asset: str) -> tuple[str, ...]:
        """该资产当前簿子里出现过的全部来源。"""
        with self._lock:
            return tuple(s for (a, s) in self._quotes.keys() if a == asset)

    def has_any(self, asset: str) -> bool:
        with self._lock:
            return asset in self._assets


class PriceAggregator:
    """按资产配置对多来源报价做校验、离群剔除与中位数聚合。"""

    def __init__(self, book: PriceBook) -> None:
        self._book = book

    def evaluate(self, config: AssetConfig, now: float) -> AggregationResult:
        """产出聚合价格；有效来源不足时抛 InsufficientValidSources。

        判定顺序（彼此可区分的拒绝原因）：
        1. 缺失（无报价）/ unconfigured（来源未配置）
        2. 过期（年龄超 max_age_seconds）
        3. 非正数
        4. 离群（相对候选中位数偏离超阈值）
        5. 剩余有效来源数 < min_valid_sources -> 资产不可用
        """
        asset = config.asset
        snap = self._book.snapshot(asset, config.sources)

        rejected: list[QuoteRejected] = []
        candidates: list[Quote] = []

        # 1) 缺失
        for source in config.sources:
            q = snap[source]
            if q is None:
                rejected.append(QuoteMissing(source, "配置来源在价格簿中无报价"))
                continue
            candidates.append(q)

        # 未配置来源：记录但不参与聚合
        configured = set(config.sources)
        for source in self._book.sources_for(asset):
            if source not in configured:
                rejected.append(
                    _UnconfiguredSource(source, "来源未在该资产配置中, 不参与聚合")
                )

        fresh: list[Quote] = []
        # 2) 过期
        for q in candidates:
            age = now - q.ts
            if age > config.max_age_seconds:
                rejected.append(QuoteExpired(q.source, age, config.max_age_seconds))
                continue
            fresh.append(q)

        positive: list[Quote] = []
        # 3) 非正数
        for q in fresh:
            if q.value <= 0:
                rejected.append(QuoteNonPositive(q.source, str(q.value)))
                continue
            positive.append(q)

        # 4) 离群（相对候选中位数；阈值严格大于才算离群）
        valid: list[Quote] = []
        if positive:
            ref = median_price([q.value for q in positive])
            for q in positive:
                deviation = abs(q.value - ref) / ref
                if deviation > config.outlier_deviation:
                    rejected.append(
                        QuoteOutlier(
                            q.source,
                            str(q.value),
                            str(ref),
                            str(deviation),
                            str(config.outlier_deviation),
                        )
                    )
                else:
                    valid.append(q)

        used = tuple(q.source for q in valid)
        rejection_records = tuple(_to_record(r) for r in rejected)

        logger.info(
            "evaluate asset=%s now=%s used=%s rejected=%s",
            asset,
            now,
            used,
            [(r.source, r.reason_code, r.detail) for r in rejection_records],
        )

        # 5) 有效来源不足
        if len(valid) < config.min_valid_sources:
            if not self._book.has_any(asset):
                raise NoPriceObservation(asset)
            raise InsufficientValidSources(
                asset, len(valid), config.min_valid_sources, rejected
            )

        with localcontext() as ctx:
            ctx.prec = 60
            price = median_price([q.value for q in valid])

        return AggregationResult(
            asset=asset,
            price=price,
            used_sources=used,
            rejected=rejection_records,
            computed_ts=now,
        )

    def try_evaluate(self, config: AssetConfig, now: float) -> AggregationResult | None:
        """同 evaluate，但无观测或有效来源不足时返回 None。"""
        try:
            return self.evaluate(config, now)
        except (InsufficientValidSources, NoPriceObservation):
            return None


class _UnconfiguredSource(QuoteRejected):
    """内部：未配置来源的拒绝（reason_code 映射为 unconfigured）。"""


_REASON_CODE[_UnconfiguredSource] = "unconfigured"


def _to_record(r: QuoteRejected) -> SourceRejection:
    code = _REASON_CODE.get(type(r), "unknown")
    return SourceRejection(source=r.source, reason_code=code, detail=r.reason)


def median_price(values: list[Decimal]) -> Decimal:
    """中位数聚合（偶数个取中间两值的算术平均）。"""
    if not values:
        raise ValueError("median_price 需要至少一个值")
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2
