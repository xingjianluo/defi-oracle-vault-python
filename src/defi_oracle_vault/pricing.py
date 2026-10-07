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
历史评估为只读，不改动任何状态。查询失败给出三种互不混淆的结论：
- NoPriceObservation：该资产从来没收到过报价；
- HistoryOutOfRetention：查询时点早于回看边界，曾有报价但已被清理；
- InsufficientValidSources：保留范围内但当时无报价(missing)/来源从未报价/
  过期/非正/离群等（逐来源原因见 rejected 明细）。
"""

from __future__ import annotations

import bisect
import logging
import math
import threading
from dataclasses import dataclass
from decimal import Decimal, localcontext

from .errors import (
    HistoryOutOfRetention,
    InsufficientValidSources,
    NoPriceObservation,
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
class HistoricalView:
    """一次历史查询在价格簿锁内得到的自洽判定依据（只读快照）。"""

    ts: float
    quotes: dict[str, Quote | None]
    pruned: frozenset[str]
    retained_starts: dict[str, float | None]
    ordered_retained_starts: tuple[float, ...]
    observed_any: bool

    def horizon_for(self, min_valid_sources: int) -> float | None:
        """回看边界（含等号）：第 min_valid_sources 小的保留起点；不足则 None。"""
        if len(self.ordered_retained_starts) < min_valid_sources:
            return None
        return self.ordered_retained_starts[min_valid_sources - 1]


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

        非有限 ts（NaN/±inf）不参与任何比较：固定返回 (None, False)，
        不依赖 NaN 比较语义，调用方不会因此得到飘忽结果或异常；
        时间合法性由服务层 _check_query_ts 进一步明确拒绝。
        """
        with self._lock:
            key = (asset, source)
            if not math.isfinite(ts):
                return None, False
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
        with self._lock:
            quotes: dict[str, Quote | None] = {}
            pruned: list[str] = []
            for s in sources:
                q, was_pruned = self.as_of(asset, s, ts)
                quotes[s] = q
                if was_pruned:
                    pruned.append(s)
            return quotes, frozenset(pruned)

    def retained_start(self, asset: str, source: str) -> float | None:
        """该来源当前保留序列里最早一条报价的 ts（回看边界的单来源依据）。

        序列为空（从未见过或已全部清理）时返回 None。
        """
        with self._lock:
            series = self._series.get((asset, source))
            return series[0].ts if series else None

    def historical_view(
        self, asset: str, sources: tuple[str, ...], ts: float
    ) -> HistoricalView:
        """在同一把锁内给出历史判定所需的全部依据（并发下自洽、可复现）。

        - quotes/pruned：各来源在 ts 时点的最近可用报价与"曾有但已清理"集合；
        - retained_starts / ordered_retained_starts：各来源当前保留序列的
          最早 ts（空序列为 None）及其升序列表，资产级回看边界即其中
          第 min_valid_sources 小者（由 HistoricalView.horizon_for 计算）；
        - observed_any：该资产是否曾摄入过任何报价。
        """
        with self._lock:
            quotes: dict[str, Quote | None] = {}
            pruned: list[str] = []
            starts: dict[str, float | None] = {}
            for s in sources:
                q, was_pruned = self.as_of(asset, s, ts)
                quotes[s] = q
                # 只在 ts 不早于该来源最早观测时才算"曾有但已清理"；
                # 早于最早观测一律属于 missing（当时确实还没有报价）。
                earliest = self._earliest.get((asset, s))
                if was_pruned and earliest is not None and ts >= earliest:
                    pruned.append(s)
                series = self._series.get((asset, s))
                starts[s] = series[0].ts if series else None
            ordered_starts = sorted(v for v in starts.values() if v is not None)
            return HistoricalView(
                ts=ts,
                quotes=quotes,
                pruned=frozenset(pruned),
                retained_starts=starts,
                ordered_retained_starts=tuple(ordered_starts),
                observed_any=asset in self._assets,
            )

    def history_horizon(
        self, asset: str, sources: tuple[str, ...], min_valid_sources: int
    ) -> float | None:
        """资产级回看边界（含等号）：能凑齐 min_valid_sources 条保留序列的最早 ts。

        即各来源保留序列最早 ts 中第 min_valid_sources 小者；不足该数量的
        非空序列时返回 None（任何时点都不可能靠保留数据凑齐）。
        """
        with self._lock:
            starts = sorted(
                series[0].ts
                for s in sources
                if (series := self._series.get((asset, s)))
            )
            if len(starts) < min_valid_sources:
                return None
            return starts[min_valid_sources - 1]

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
        报价，绝不使用更晚的报价（不泄漏未来信息）；绝不拿更早或更晚的
        价格顶替。失败结论按**固定、可复现**的顺序分类：

        1. 该资产从未收到过任何报价 -> ``NoPriceObservation``；
        2. 查询时点早于资产回看边界（ts < horizon，边界含等号），即至少有
           一个来源在 ts 之前确有报价、但能凑齐最少来源数的保留序列都还没
           开始 -> ``HistoryOutOfRetention``（"数据已被清理"，可单独识别）；
        3. 其余有效来源不足 -> ``InsufficientValidSources``，拒绝明细逐来源
           给出原因：``missing``（当时还没有报价/该来源从没报过价）、
           ``pruned``（曾有但该来源已被清理）、``expired`` 等。

        因此"超出保留范围""当时没有报价""资产从无观测"是三种互不混淆的结论。
        """
        view = self._book.historical_view(config.asset, config.sources, ts)
        return self._evaluate_snapshot(
            config, view.quotes, view.pruned, ts, label="evaluate_as_of", view=view
        )

    def _evaluate_snapshot(
        self,
        config: AssetConfig,
        snap: dict[str, Quote | None],
        pruned: frozenset[str],
        now: float,
        label: str,
        view: HistoricalView | None = None,
    ) -> AggregationResult:
        """对一份报价快照执行统一判定管线。

        判定顺序（彼此可区分的拒绝原因）：
        1. 缺失（该时点无报价）/ pruned（已被清理）/ unconfigured（来源未配置）
        2. 过期（相对判定时点年龄超 max_age_seconds）
        3. 非正数
        4. 离群（相对候选中位数偏离超阈值）
        5. 剩余有效来源数 < min_valid_sources，按以下固定顺序给出资产级结论：
           a) view 非 None（历史回看）且资产从无任何观测 -> NoPriceObservation；
           b) view 非 None 且存在"曾有但已清理"的来源、且 ts 早于回看边界
              （保留范围内可用序列数不足 min_valid_sources）
              -> HistoryOutOfRetention；
           c) 实时路径资产从无观测 -> NoPriceObservation；
           d) 其余 -> InsufficientValidSources（逐来源原因可解释）。
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
            "%s asset=%s now=%s used=%s rejected=%s",
            label,
            asset,
            now,
            used,
            [(r.source, r.reason_code, r.detail) for r in rejection_records],
        )

        # 5) 有效来源不足：资产级失败结论按固定顺序分类
        if len(valid) < config.min_valid_sources:
            if view is not None and not view.observed_any:
                raise NoPriceObservation(asset)
            if view is not None and view.pruned:
                horizon = view.horizon_for(config.min_valid_sources)
                if horizon is not None and view.ts < horizon:
                    available_series = sum(
                        1 for start in view.retained_starts.values()
                        if start is not None and start <= view.ts
                    )
                    pruned_sources = tuple(sorted(view.pruned))
                    logger.warning(
                        "%s OUT_OF_RETENTION asset=%s ts=%s horizon=%s(含等号) "
                        "retained_available=%d/%d pruned_sources=%s rejected=%s",
                        label,
                        asset,
                        view.ts,
                        horizon,
                        available_series,
                        config.min_valid_sources,
                        pruned_sources,
                        [
                            (r.source, r.reason_code, r.detail)
                            for r in rejection_records
                        ],
                    )
                    raise HistoryOutOfRetention(
                        asset=asset,
                        ts=view.ts,
                        horizon=horizon,
                        required=config.min_valid_sources,
                        available_series=available_series,
                        pruned_sources=pruned_sources,
                        rejected=rejected,
                    )
            if view is None and not self._book.has_any(asset):
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
        """同 evaluate_as_of，但无观测、超出保留范围或有效来源不足时返回 None。"""
        try:
            return self.evaluate_as_of(config, ts)
        except (
            InsufficientValidSources,
            NoPriceObservation,
            HistoryOutOfRetention,
        ):
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
