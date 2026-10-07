"""多价格源摄入与聚合。

拒绝原因彼此可区分：
- missing:       配置了但该时点没有任何报价（当时确实还没有）
- pruned:        该时点之前确有报价，但已超出保留窗口被清理（与 missing 可区分）
- expired:       报价年龄超过 max_age_seconds
- non_positive:  报价 <= 0
- outlier:       相对候选中位数偏离超过 outlier_deviation
- unconfigured:  报价来自该资产未配置的来源，不参与聚合

有效来源不足时抛 InsufficientValidSources，绝不回退陈旧/默认价格。

历史回看：PriceBook 按 (资产, 来源) 保留有界报价序列（见 HistoryRetention），
evaluate_as_of 只用 ts <= 查询时点的报价做判定，绝不泄漏未来信息；
历史评估为只读，不改动任何状态。
"""

from __future__ import annotations

import bisect
import logging
import threading
from dataclasses import dataclass
from decimal import Decimal, localcontext

from .errors import (
    InsufficientValidSources,
    NoPriceObservation,
    PriceHistoryPruned,
    QuoteExpired,
    QuoteMissing,
    QuoteNonPositive,
    QuoteOutlier,
    QuotePruned,
    QuoteRejected,
)
from .models import AggregationResult, AssetConfig, HistoryRetention, Quote, SourceRejection

logger = logging.getLogger(__name__)

_REASON_CODE = {
    QuoteMissing: "missing",
    QuotePruned: "pruned",
    QuoteExpired: "expired",
    QuoteNonPositive: "non_positive",
    QuoteOutlier: "outlier",
}


class _UnconfiguredSource(QuoteRejected):
    """内部：未配置来源的拒绝（reason_code 映射为 unconfigured）。"""


_REASON_CODE[_UnconfiguredSource] = "unconfigured"


@dataclass(frozen=True)
class _AsOfView:
    """一次历史时点判定所需的全部信息（单次加锁原子采集，并发下不漂移）。

    - quotes:        配置来源在 ts（含）之前最近的一条保留报价（无则 None）
    - pruned:        该时点之前确有报价但已被清理的配置来源集合
    - unconfigured:  在 ts（含）之前曾报过价的未配置来源（不泄漏未来信息）
    - observed:      该资产在 ts（含）之前是否曾观测到任何报价
    - horizons:      各配置来源仍保留的最早报价 ts（用于计算可回看边界）
    """

    quotes: dict[str, Quote | None]
    pruned: frozenset[str]
    unconfigured: frozenset[str]
    observed: bool
    horizons: dict[str, float]


class PriceBook:
    """线程安全的本地价格簿：每个 (资产, 来源) 保留有界历史报价序列。

    保留策略由 HistoryRetention 决定，摄入时滚动清理，内存有界。
    每个序列的最早观测时间被永久记住，用于区分
    "查询时点之前确实没有报价"(missing) 与 "报价已被清理"(pruned)。
    """

    def __init__(self, retention: HistoryRetention | None = None) -> None:
        self._retention = retention or HistoryRetention()
        # (asset, source) -> 按 (ts, 到达顺序) 升序的报价序列
        self._series: dict[tuple[str, str], list[Quote]] = {}
        # (asset, source) -> 历史最早观测 ts（不随清理丢失）
        self._earliest: dict[tuple[str, str], float] = {}
        self._assets: set[str] = set()
        self._lock = threading.RLock()

    @property
    def retention(self) -> HistoryRetention:
        return self._retention

    def ingest(self, quote: Quote) -> None:
        """摄入一条报价（同 ts 重复摄入时后到者优先），并按保留策略清理。"""
        with self._lock:
            key = (quote.asset, quote.source)
            series = self._series.setdefault(key, [])
            bisect.insort(series, quote, key=lambda q: q.ts)
            prev = self._earliest.get(key)
            if prev is None or quote.ts < prev:
                self._earliest[key] = quote.ts
            self._assets.add(quote.asset)
            self._prune(key, series)
            logger.info(
                "ingest asset=%s source=%s value=%s ts=%s retained=%d",
                quote.asset,
                quote.source,
                quote.value,
                quote.ts,
                len(series),
            )

    def _prune(self, key: tuple[str, str], series: list[Quote]) -> None:
        """按保留策略清理序列：年龄窗口与条数上限同时生效。"""
        r = self._retention
        if r.max_age_seconds is not None and series:
            cutoff = series[-1].ts - r.max_age_seconds
            # 保留 newest_ts - ts <= max_age_seconds 的条目（边界含等号）
            drop = bisect.bisect_left(series, cutoff, key=lambda q: q.ts)
            if drop:
                del series[:drop]
                logger.debug("prune age key=%s dropped=%d cutoff=%s", key, drop, cutoff)
        if r.max_entries_per_source is not None:
            overflow = len(series) - r.max_entries_per_source
            if overflow > 0:
                del series[:overflow]
                logger.debug("prune count key=%s dropped=%d", key, overflow)

    def latest(self, asset: str, source: str) -> Quote | None:
        """读取某资产某来源的最新报价（无则 None）。"""
        with self._lock:
            series = self._series.get((asset, source))
            return series[-1] if series else None

    def snapshot(self, asset: str, sources: tuple[str, ...]) -> dict[str, Quote | None]:
        """原子地读取多来源最新报价。"""
        with self._lock:
            return {s: self._latest_locked(asset, s) for s in sources}

    def _latest_locked(self, asset: str, source: str) -> Quote | None:
        series = self._series.get((asset, source))
        return series[-1] if series else None

    def as_of(
        self, asset: str, source: str, ts: float
    ) -> tuple[Quote | None, bool]:
        """读取某来源在 ts 时点（含）之前最近的一条保留报价。

        返回 (quote, pruned)：
        - quote 非 None：该时点可用的最近报价（q.ts <= ts）；
        - quote 为 None 且 pruned=False：该时点之前确实没有任何报价；
        - quote 为 None 且 pruned=True：该时点之前确有报价但已被清理。
        """
        with self._lock:
            key = (asset, source)
            earliest = self._earliest.get(key)
            if earliest is None or ts < earliest:
                return None, False
            series = self._series.get(key)
            if not series:
                return None, True
            idx = bisect.bisect_right(series, ts, key=lambda q: q.ts) - 1
            if idx >= 0:
                return series[idx], False
            return None, True

    def as_of_snapshot(
        self, asset: str, sources: tuple[str, ...], ts: float
    ) -> tuple[dict[str, Quote | None], frozenset[str]]:
        """原子地读取多来源在 ts 时点的可用报价与被清理来源集合。"""
        view = self.as_of_view(asset, sources, ts)
        return view.quotes, view.pruned

    def as_of_view(
        self, asset: str, sources: tuple[str, ...], ts: float
    ) -> _AsOfView:
        """单次持锁原子采集历史时点判定所需的全部信息。

        快照内各字段互相对应于同一个簿子状态，因此持续摄入新报价的同时
        反复查询同一时点，拒绝明细与结论不会在请求之间漂移。
        """
        with self._lock:
            quotes: dict[str, Quote | None] = {}
            pruned: list[str] = []
            horizons: dict[str, float] = {}
            observed = False
            for s in sources:
                key = (asset, s)
                earliest = self._earliest.get(key)
                series = self._series.get(key)
                if earliest is not None and earliest <= ts:
                    observed = True
                if earliest is None or ts < earliest:
                    # 该时点之前从未见过此来源的报价（不是被清理）
                    quotes[s] = None
                    continue
                if not series:
                    # 曾有报价但序列已被全部清理
                    quotes[s] = None
                    pruned.append(s)
                    continue
                horizons[s] = series[0].ts
                idx = bisect.bisect_right(series, ts, key=lambda q: q.ts) - 1
                if idx >= 0:
                    quotes[s] = series[idx]
                else:
                    # 曾有报价（最早观测 <= ts），但保留下来的都晚于 ts：已被清理
                    quotes[s] = None
                    pruned.append(s)

            unconf: list[str] = []
            for (a, s), series in self._series.items():
                if a != asset or s in sources:
                    continue
                # 只统计 ts（含）之前已经到过的未配置来源，避免泄漏未来信息
                if series and series[0].ts <= ts:
                    unconf.append(s)
                if self._earliest.get((a, s)) is not None and self._earliest[(a, s)] <= ts:
                    observed = True

            return _AsOfView(
                quotes=quotes,
                pruned=frozenset(pruned),
                unconfigured=frozenset(unconf),
                observed=observed,
                horizons=horizons,
            )

    def sources_for(self, asset: str) -> tuple[str, ...]:
        """该资产当前簿子里出现过的全部来源。"""
        with self._lock:
            return tuple(s for (a, s) in self._series.keys() if a == asset)

    def has_any(self, asset: str) -> bool:
        with self._lock:
            return asset in self._assets


class PriceAggregator:
    """按资产配置对多来源报价做校验、离群剔除与中位数聚合。"""

    def __init__(self, book: PriceBook) -> None:
        self._book = book

    def evaluate(self, config: AssetConfig, now: float) -> AggregationResult:
        """产出当前聚合价格；有效来源不足时抛 InsufficientValidSources。"""
        snap = self._book.snapshot(config.asset, config.sources)
        return self._evaluate_snapshot(config, snap, frozenset(), now, label="evaluate")

    def evaluate_as_of(self, config: AssetConfig, ts: float) -> AggregationResult:
        """产出过去某时点的聚合价格（只读，不改动任何状态）。

        判定口径与 evaluate 完全一致，但只使用 ts 时点（含）之前已存在的
        报价，绝不使用更晚的报价（不泄漏未来信息），从不拿更早或更晚的价格
        顶替。三种失败彼此独立、可单独识别：
        - PriceHistoryPruned：配置来源中确曾有报价但已超出保留范围被清理
          （HTTP 410，独立 error_code）；边界含等号——ts 恰好等于仍保留的
          最早一条报价时该报价可用，只有更早才判超出保留范围；
        - InsufficientValidSources：在保留范围内，但当时来源不足/全部被拒
          （missing/expired/non_positive/outlier）；
        - NoPriceObservation：该资产在该时点之前从未收到过任何报价。
        """
        view = self._book.as_of_view(config.asset, config.sources, ts)
        return self._evaluate_snapshot(
            config,
            view.quotes,
            view.pruned,
            ts,
            label="evaluate_as_of",
            unconfigured=view.unconfigured,
            observed=view.observed,
            horizons=view.horizons,
        )

    def _evaluate_snapshot(
        self,
        config: AssetConfig,
        snap: dict[str, Quote | None],
        pruned: frozenset[str],
        now: float,
        label: str,
        unconfigured: frozenset[str] | None = None,
        observed: bool | None = None,
        horizons: dict[str, float] | None = None,
    ) -> AggregationResult:
        """对一份报价快照执行统一判定管线。

        判定顺序（彼此可区分的拒绝原因）：
        1. 缺失（该时点无报价）/ pruned（已被清理）/ unconfigured（来源未配置）
        2. 过期（相对判定时点年龄超 max_age_seconds）
        3. 非正数
        4. 离群（相对候选中位数偏离超阈值）
        5. 剩余有效来源数 < min_valid_sources -> 资产不可用
           历史路径上若存在 pruned 来源，优先抛 PriceHistoryPruned。
        """
        asset = config.asset

        rejected: list[QuoteRejected] = []
        candidates: list[Quote] = []

        # 1) 缺失 / 已清理
        for source in config.sources:
            q = snap[source]
            if q is None:
                if source in pruned:
                    rejected.append(
                        QuotePruned(source, "该时点之前确有报价, 但已超出保留窗口")
                    )
                else:
                    rejected.append(QuoteMissing(source, "配置来源在该时点无报价"))
                continue
            candidates.append(q)

        # 未配置来源：记录但不参与聚合
        if unconfigured is None:
            unconfigured = frozenset(self._book.sources_for(asset)) - set(config.sources)
        for source in sorted(unconfigured):
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
            "%s asset=%s now=%s used=%s rejected=%s",
            label,
            asset,
            now,
            used,
            [(r.source, r.reason_code, r.detail) for r in rejection_records],
        )

        # 5) 有效来源不足
        if len(valid) < config.min_valid_sources:
            if observed is None:
                observed = self._book.has_any(asset)
            # 历史路径上有来源曾报过价但已被清理：这是"超出保留范围"，
            # 独立于"当时来源不足"，结论稳定（pruned 集合在原子快照中确定）
            if pruned:
                horizon = min(horizons.values()) if horizons else None
                raise PriceHistoryPruned(
                    asset, now, horizon, rejected, len(valid), config.min_valid_sources
                )
            if not observed:
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

    def try_evaluate_as_of(
        self, config: AssetConfig, ts: float
    ) -> AggregationResult | None:
        """同 evaluate_as_of，但无观测/超出保留范围/有效来源不足时返回 None。"""
        try:
            return self.evaluate_as_of(config, ts)
        except (InsufficientValidSources, NoPriceObservation, PriceHistoryPruned):
            return None


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
