"""多价格源摄入与聚合。

拒绝原因彼此可区分：
- missing:       配置了但当前没有任何报价
- expired:       报价年龄超过 max_age_seconds
- non_positive:  报价 <= 0
- outlier:       相对候选中位数偏离超过 outlier_deviation
- unconfigured:  报价来自该资产未配置的来源，不参与聚合

有效来源不足时抛 InsufficientValidSources，绝不回退陈旧/默认价格。

历史留存（PriceBook）：
- 每个 (资产, 来源) 保留一段**有界**的报价历史，双上限：
  1. 时间窗 retention_seconds：以该资产"最晚摄入报价"的时间为锚，
     只保留 ts >= anchor - retention_seconds（边界保留，严格早于才淘汰）；
  2. 条数上限 max_quotes_per_source：超过时每来源只保留最新的 N 条。
- 任一上限触发即淘汰，因此内存占用有明确上界；乱序迟到且越界的报价
  摄入后立即被淘汰。每个来源自己的最新报价恒在保留范围内（年龄为 0、
  且条数裁剪保留尾部），所以"查询时点恰好落在留存边界/恰好等于某条报价
  时刻"的结论一定可复现；更早的时点明确抛 HistoryOutOfRetention，
  与 NoPriceObservation（留存可覆盖且当时确实没有任何报价）区分。
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
    InvalidHistoryQuery,
    NoPriceObservation,
    QuoteExpired,
    QuoteMissing,
    QuoteNonPositive,
    QuoteOutlier,
    QuoteRejected,
)
from .models import (
    AggregationResult,
    AssetConfig,
    HistoricalAggregationResult,
    Quote,
    SourceRejection,
)

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

DEFAULT_RETENTION_SECONDS = 3600.0
DEFAULT_MAX_QUOTES_PER_SOURCE = 1000


@dataclass(frozen=True)
class _HistSnapshot:
    """历史聚合用的一致性时点快照（持锁构造后在锁外计算）。"""

    asset: str
    ts: float
    ever_seen: bool
    watermark: float | None
    cutoff: float | None
    max_quotes: int | None
    # source -> 该来源保留的最早一条 ts（空列表表示见过但已全部淘汰）
    oldest: dict[str, float | None]
    # 序列当前长度（用于判断是否顶到条数上限）
    lengths: dict[str, int]
    # source -> ts <= 查询时点的最新一条 Quote（没有则 None）
    chosen: dict[str, Quote | None]

    def horizon(self) -> float | None:
        """该资产历史查询可确信回答的最早时点（留存下界）。

        分来源讨论（取各来源下界的最小值）：
        - 时间窗保留且序列**未**顶到条数上限：[cutoff, ...) 内若有报价必存活，
          故从 cutoff（含边界）起"当时无报价"的结论可确信；
        - 序列已顶到条数上限（或仅用条数策略）：更早的报价可能是被条数
          上限淘汰的，无法与"从未存在"区分，只能从最早保留报价 ts0 起回答；
        - 序列已空但来源见过报价：只可能是时间窗裁剪，下界为 cutoff。
        严格早于下界的时点抛 HistoryOutOfRetention。
        """
        if not self.ever_seen:
            return None
        lowers: list[float] = []
        for source, ts0 in self.oldest.items():
            capped = (
                self.max_quotes is not None
                and self.lengths.get(source, 0) >= self.max_quotes
            )
            if ts0 is None:
                if self.cutoff is not None:
                    lowers.append(self.cutoff)
            elif self.cutoff is None or capped:
                lowers.append(ts0)
            else:
                lowers.append(self.cutoff)
        return min(lowers) if lowers else None


class PriceBook:
    """线程安全的本地价格簿：保存每个资产每个来源一段有界的报价历史。"""

    def __init__(
        self,
        retention_seconds: float | None = DEFAULT_RETENTION_SECONDS,
        max_quotes_per_source: int | None = DEFAULT_MAX_QUOTES_PER_SOURCE,
    ) -> None:
        if retention_seconds is not None and (
            not _finite(retention_seconds) or retention_seconds <= 0
        ):
            raise ValueError("retention_seconds 必须为有限正数或 None")
        if max_quotes_per_source is not None and max_quotes_per_source < 1:
            raise ValueError("max_quotes_per_source 必须 >= 1 或 None")
        self._retention_seconds = retention_seconds
        self._max_quotes = max_quotes_per_source
        # (asset, source) -> 按 ts 升序的报价列表（有界）
        self._hist: dict[tuple[str, str], list[Quote]] = {}
        self._assets: set[str] = set()
        # asset -> 该资产见过的最晚摄入时间（留存锚点，乱序摄入也不回退）
        self._watermarks: dict[str, float] = {}
        self._lock = threading.RLock()

    # ---- 写入 ----

    def ingest(self, quote: Quote) -> None:
        """摄入一条报价：按 ts 有序插入；同 (来源, ts) 覆盖；随后按双上限裁剪。"""
        with self._lock:
            asset = quote.asset
            key = (asset, quote.source)
            series = self._hist.setdefault(key, [])
            ts = quote.ts
            pos = bisect.bisect_left(series, ts, key=lambda q: q.ts)
            if pos < len(series) and series[pos].ts == ts:
                series[pos] = quote  # 同点覆盖，保持幂等
            else:
                series.insert(pos, quote)
            self._assets.add(asset)
            wm = self._watermarks.get(asset)
            advanced = wm is None or ts > wm
            self._watermarks[asset] = ts if advanced else (wm if wm is not None else ts)
            # 当前来源始终裁剪；水印前进时，时间窗可能收紧，需裁剪该资产全部来源
            for (a, source), other in self._hist.items():
                if a == asset and (advanced or source == quote.source):
                    self._prune(asset, (a, source), other)
            logger.info(
                "ingest asset=%s source=%s value=%s ts=%s retained=%d",
                asset,
                quote.source,
                quote.value,
                quote.ts,
                len(series),
            )

    def _prune(
        self,
        asset: str,
        key: tuple[str, str],
        series: list[Quote],
    ) -> None:
        """按时间窗与条数上限裁剪单个来源序列（调用方持锁）。"""
        if self._retention_seconds is not None:
            cutoff = self._watermarks[asset] - self._retention_seconds
            i = bisect.bisect_left(series, cutoff, key=lambda q: q.ts)
            if i:
                del series[:i]
        if self._max_quotes is not None and len(series) > self._max_quotes:
            del series[: len(series) - self._max_quotes]

    # ---- 实时读取（语义与历史化前一致：最新一条） ----

    def latest(self, asset: str, source: str) -> Quote | None:
        """读取某资产某来源的最新报价（无则 None）。"""
        with self._lock:
            series = self._hist.get((asset, source))
            return series[-1] if series else None

    def snapshot(self, asset: str, sources: tuple[str, ...]) -> dict[str, Quote | None]:
        """原子地读取多来源最新报价。"""
        with self._lock:
            result: dict[str, Quote | None] = {}
            for s in sources:
                series = self._hist.get((asset, s))
                result[s] = series[-1] if series else None
            return result

    def sources_for(self, asset: str) -> tuple[str, ...]:
        """该资产当前簿子里仍保留有报价的全部来源。"""
        with self._lock:
            return tuple(
                s for (a, s), series in self._hist.items() if a == asset and series
            )

    def has_any(self, asset: str) -> bool:
        with self._lock:
            return asset in self._assets

    # ---- 历史读取 ----

    def quote_at(self, asset: str, source: str, ts: float) -> Quote | None:
        """该来源在 ts 时点有效的最新报价（ts' <= ts），无则 None。"""
        with self._lock:
            series = self._hist.get((asset, source))
            if not series:
                return None
            i = bisect.bisect_right(series, ts, key=lambda q: q.ts) - 1
            return series[i] if i >= 0 else None

    def retained_count(self, asset: str, source: str) -> int:
        with self._lock:
            return len(self._hist.get((asset, source), ()))

    def oldest_retained_ts(self, asset: str) -> float | None:
        """该资产全部来源中保留的最早一条报价时间（无任何保留报价时 None）。"""
        with self._lock:
            vals = [
                series[0].ts
                for (a, _s), series in self._hist.items()
                if a == asset and series
            ]
            return min(vals) if vals else None

    def historical_snapshot(self, asset: str, ts: float) -> _HistSnapshot:
        """原子地截取 ts 时点的一致性视图（只含 ts' <= ts 的报价，不泄漏未来）。"""
        with self._lock:
            oldest: dict[str, float | None] = {}
            lengths: dict[str, int] = {}
            chosen: dict[str, Quote | None] = {}
            watermark = self._watermarks.get(asset)
            cutoff = (
                watermark - self._retention_seconds
                if watermark is not None and self._retention_seconds is not None
                else None
            )
            for (a, source), series in self._hist.items():
                if a != asset:
                    continue
                oldest[source] = series[0].ts if series else None
                lengths[source] = len(series)
                if series:
                    i = bisect.bisect_right(series, ts, key=lambda q: q.ts) - 1
                    chosen[source] = series[i] if i >= 0 else None
                else:
                    chosen[source] = None
            return _HistSnapshot(
                asset=asset,
                ts=ts,
                ever_seen=asset in self._assets,
                watermark=watermark,
                cutoff=cutoff,
                max_quotes=self._max_quotes,
                oldest=dict(oldest),
                lengths=dict(lengths),
                chosen=dict(chosen),
            )


def _finite(ts: float) -> bool:
    return not (math.isnan(ts) or math.isinf(ts))


def _pipeline(
    config: AssetConfig,
    ts: float,
    chosen: dict[str, Quote | None],
    unconfigured_present: tuple[str, ...],
    *,
    tag: str,
) -> tuple[list[Quote], list[QuoteRejected], tuple[SourceRejection, ...]]:
    """实时与历史共用的校验/离群流水线。

    chosen 只含配置来源 -> ts 时点有效的最新报价；
    unconfigured_present 为当时确有报价可见的未配置来源。
    返回 (有效报价, 拒绝异常列表, 拒绝记录)。有效来源是否足够由调用方判定。
    """
    rejected: list[QuoteRejected] = []
    candidates: list[Quote] = []

    # 1) 缺失
    for source in config.sources:
        q = chosen.get(source)
        if q is None:
            rejected.append(QuoteMissing(source, "配置来源在价格簿中无报价"))
            continue
        candidates.append(q)

    # 未配置来源：当时有可见报价才记录，不参与聚合
    for source in unconfigured_present:
        rejected.append(
            _UnconfiguredSource(source, "来源未在该资产配置中, 不参与聚合")
        )

    fresh: list[Quote] = []
    # 2) 过期（年龄严格大于 max_age_seconds）
    for q in candidates:
        age = ts - q.ts
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

    records = tuple(_to_record(r) for r in rejected)
    logger.info(
        "%s asset=%s ts=%s used=%s rejected=%s",
        tag,
        config.asset,
        ts,
        tuple(q.source for q in valid),
        [(r.source, r.reason_code, r.detail) for r in records],
    )
    return valid, rejected, records


class PriceAggregator:
    """按资产配置对多来源报价做校验、离群剔除与中位数聚合。"""

    def __init__(self, book: PriceBook) -> None:
        self._book = book

    def evaluate(self, config: AssetConfig, now: float) -> AggregationResult:
        """产出实时聚合价格；有效来源不足时抛 InsufficientValidSources。

        判定顺序（彼此可区分的拒绝原因）：
        1. 缺失（无报价）/ unconfigured（来源未配置）
        2. 过期（年龄超 max_age_seconds）
        3. 非正数
        4. 离群（相对候选中位数偏离超阈值）
        5. 剩余有效来源数 < min_valid_sources -> 资产不可用
        """
        asset = config.asset
        snap = self._book.snapshot(asset, config.sources)
        configured = set(config.sources)
        unconfigured = tuple(
            s for s in self._book.sources_for(asset) if s not in configured
        )
        valid, rejected, records = _pipeline(
            config, now, snap, unconfigured, tag="evaluate"
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
            used_sources=tuple(q.source for q in valid),
            rejected=records,
            computed_ts=now,
        )

    def evaluate_at(
        self, config: AssetConfig, ts: float
    ) -> HistoricalAggregationResult:
        """只读回放 ts 时点的聚合价，判定口径与 evaluate 完全一致。

        - 只使用 ts' <= ts 的报价（绝不泄漏未来信息），不触碰熔断状态；
        - ts 必须为有限数，否则 InvalidHistoryQuery；
        - ts 早于该资产留存下界 -> HistoryOutOfRetention（数据已淘汰）；
        - 留存可覆盖、但 ts 时点确实没有任何报价 -> NoPriceObservation；
        - 有效来源不足 -> InsufficientValidSources（与实时口径相同）。
        同一时点反复查询结果一致，且不影响并发进行的写入。
        """
        if not _finite(ts):
            raise InvalidHistoryQuery(f"历史查询时点必须为有限数, 收到 {ts!r}")
        asset = config.asset

        # 持锁只做一次一致性快照，随后的判定全部在锁外完成（不拖慢写入）
        hsnap = self._book.historical_snapshot(asset, ts)
        if not hsnap.ever_seen:
            raise NoPriceObservation(asset)
        horizon = hsnap.horizon()
        if horizon is not None and ts < horizon:
            raise HistoryOutOfRetention(asset, ts, horizon)

        chosen = {s: hsnap.chosen.get(s) for s in config.sources}
        configured = set(config.sources)
        unconfigured = tuple(
            s
            for s, q in hsnap.chosen.items()
            if s not in configured and q is not None
        )
        observed = any(q is not None for q in hsnap.chosen.values())
        if not observed:
            # 留存可覆盖该时点，但那一刻任何来源都还没有报价
            raise NoPriceObservation(asset)

        valid, rejected, records = _pipeline(
            config, ts, chosen, unconfigured, tag="evaluate_at"
        )
        if len(valid) < config.min_valid_sources:
            raise InsufficientValidSources(
                asset, len(valid), config.min_valid_sources, rejected
            )

        with localcontext() as ctx:
            ctx.prec = 60
            price = median_price([q.value for q in valid])

        return HistoricalAggregationResult(
            asset=asset,
            queried_ts=ts,
            price=price,
            used_sources=tuple(q.source for q in valid),
            rejected=records,
            oldest_retained_ts=self._book.oldest_retained_ts(asset),
        )

    def try_evaluate_at(
        self, config: AssetConfig, ts: float
    ) -> HistoricalAggregationResult | None:
        """同 evaluate_at，但无观测或有效来源不足时返回 None。

        注意：HistoryOutOfRetention / InvalidHistoryQuery 仍会抛出，
        它们是"无法回答"而不是"当时不可用"。
        """
        try:
            return self.evaluate_at(config, ts)
        except (InsufficientValidSources, NoPriceObservation):
            return None

    def try_evaluate(self, config: AssetConfig, now: float) -> AggregationResult | None:
        """同 evaluate，但无观测或有效来源不足时返回 None。"""
        try:
            return self.evaluate(config, now)
        except (InsufficientValidSources, NoPriceObservation):
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
