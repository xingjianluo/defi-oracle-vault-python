"""历史聚合回放：有界留存、不泄漏未来、不可用/无报价/越界可区分、边界与拒绝原因。"""

import logging
from decimal import Decimal

import pytest

from defi_oracle_vault.errors import (
    HistoryOutOfRetention,
    InsufficientValidSources,
    InvalidHistoryQuery,
    NoPriceObservation,
    QuoteExpired,
    QuoteMissing,
    QuoteNonPositive,
)
from defi_oracle_vault.models import AssetConfig, Quote
from defi_oracle_vault.pricing import PriceAggregator, PriceBook


def make_cfg(**kw):
    defaults = {
        "asset": "BTC",
        "sources": ("a", "b", "c"),
        "min_valid_sources": 2,
        "max_age_seconds": 10.0,
        "outlier_deviation": Decimal("0.05"),
    }
    defaults.update(kw)
    return AssetConfig(**defaults)


def test_retention_time_window_bounds_memory():
    # 时间窗 100s：超过窗口的老报价被淘汰，每个来源条数有上界
    book = PriceBook(retention_seconds=100.0, max_quotes_per_source=1000)
    agg = PriceAggregator(book)
    cfg = make_cfg()
    book.ingest(Quote("BTC", "a", Decimal(1), 0))
    book.ingest(Quote("BTC", "b", Decimal(2), 0))
    # 摄入一个远晚的锚点：t=0 的报价越界被淘汰
    book.ingest(Quote("BTC", "a", Decimal(3), 200))
    assert book.retained_count("BTC", "a") == 1
    assert book.retained_count("BTC", "b") == 0
    assert book.oldest_retained_ts("BTC") == 200
    # 越界时点查不到 -> HistoryOutOfRetention（而非笼统不可用）
    with pytest.raises(HistoryOutOfRetention):
        agg.evaluate_at(cfg, 0)


def test_retention_count_cap_bounds_memory():
    # 只配条数上限：每来源最多 3 条，内存有硬上界
    book = PriceBook(retention_seconds=None, max_quotes_per_source=3)
    agg = PriceAggregator(book)
    cfg = make_cfg(min_valid_sources=1)
    for t in range(10):
        book.ingest(Quote("BTC", "a", Decimal(100 + t), float(t)))
    assert book.retained_count("BTC", "a") == 3
    # 时间窗仍存在（100s 默认）且未顶到条数上限的序列本不存在；这里来源 a
    # 已顶到条数上限，其下界是最早保留报价 t=7；t=6 无法回答
    with pytest.raises(HistoryOutOfRetention) as ei:
        agg.evaluate_at(cfg, 6.0)
    assert ei.value.horizon_ts == 7.0
    # 最早保留点本身可回答
    assert agg.evaluate_at(cfg, 7.0).price == Decimal(107)


def test_retention_boundary_exactly_at_cutoff_is_answerable():
    # cutoff = 200 - 100 = 100：边界保留，cutoff 本身可回答；再早一毫秒不可回答
    book = PriceBook(retention_seconds=100.0, max_quotes_per_source=None)
    agg = PriceAggregator(book)
    cfg = make_cfg(sources=("a",), min_valid_sources=1)
    book.ingest(Quote("BTC", "a", Decimal(1), 200))
    with pytest.raises(NoPriceObservation):
        agg.evaluate_at(cfg, 100)  # 边界可回答，且当时确无报价
    with pytest.raises(HistoryOutOfRetention):
        agg.evaluate_at(cfg, 99.999)


def test_future_quotes_never_leak_into_past():
    book = PriceBook(retention_seconds=10000.0)
    agg = PriceAggregator(book)
    cfg = make_cfg()
    book.ingest(Quote("BTC", "a", Decimal(100), 10))
    book.ingest(Quote("BTC", "b", Decimal(101), 10))
    # 未来的报价（即使已摄入）不得用于过去时点
    book.ingest(Quote("BTC", "a", Decimal(999), 50))
    book.ingest(Quote("BTC", "b", Decimal(998), 50))
    r = agg.evaluate_at(cfg, 20)
    assert r.price == Decimal("100.5")
    assert set(r.used_sources) == {"a", "b"}
    # 同一时点反复查询结果一致
    r2 = agg.evaluate_at(cfg, 20)
    assert r2 == r


def test_no_observation_vs_out_of_retention_are_distinct():
    book = PriceBook(retention_seconds=100.0)
    agg = PriceAggregator(book)
    cfg = make_cfg(sources=("a", "b"), min_valid_sources=1)
    book.ingest(Quote("BTC", "a", Decimal(1), 100))
    # t=50 在留存窗内但当时报价还没产生 -> 明确"无报价"
    with pytest.raises(NoPriceObservation):
        agg.evaluate_at(cfg, 50)
    # t=-1 早于 cutoff=0 -> 数据可能已被淘汰，无法回答
    with pytest.raises(HistoryOutOfRetention):
        agg.evaluate_at(cfg, -1)
    # 资产从未摄入过 -> NoPriceObservation
    with pytest.raises(NoPriceObservation):
        agg.evaluate_at(make_cfg(asset="ETH", sources=("a",)), 50)


def test_insufficient_valid_sources_at_point_in_time(caplog):
    book = PriceBook(retention_seconds=1000.0)
    agg = PriceAggregator(book)
    cfg = make_cfg(sources=("a", "b", "c"), min_valid_sources=2)
    # t=10 只有一源有效 -> 当时明确不可用，不许用更早/更晚价格顶替
    book.ingest(Quote("BTC", "a", Decimal(100), 10))
    book.ingest(Quote("BTC", "b", Decimal(100), 100))  # 更晚，禁止倒推
    with caplog.at_level(logging.INFO, logger="defi_oracle_vault.pricing"), pytest.raises(
        InsufficientValidSources
    ) as ei:
        agg.evaluate_at(cfg, 10)
    assert ei.value.valid == 1 and ei.value.required == 2
    assert any(isinstance(x, QuoteMissing) for x in ei.value.rejected)
    # 日志能看出喂进去的判定（evaluate_at 与拒绝原因）
    assert "evaluate_at asset=BTC" in caplog.text
    assert "missing" in caplog.text


def test_quote_exactly_at_queried_ts_is_used():
    book = PriceBook(retention_seconds=1000.0)
    agg = PriceAggregator(book)
    cfg = make_cfg()
    book.ingest(Quote("BTC", "a", Decimal(100), 10))
    book.ingest(Quote("BTC", "b", Decimal(101), 10))
    # 查询时点恰好等于报价时刻（ts' <= ts）：该报价必须生效
    r = agg.evaluate_at(cfg, 10)
    assert r.price == Decimal("100.5")
    # 早一毫秒：当时还没有报价
    with pytest.raises(NoPriceObservation):
        agg.evaluate_at(cfg, 9.999)


def test_history_rejection_reasons_match_live_semantics(caplog):
    """过期/非正/离群/缺失 在历史回放中与实时口径完全一致，且同源可叠加记录。"""
    book = PriceBook(retention_seconds=1000.0)
    agg = PriceAggregator(book)
    cfg = make_cfg(sources=("a", "b", "c"), min_valid_sources=2, max_age_seconds=5.0)
    # t=10 视角：a 过期（ts=0），b 非正，c 有效
    book.ingest(Quote("BTC", "a", Decimal(100), 0))
    book.ingest(Quote("BTC", "b", Decimal(-1), 10))
    book.ingest(Quote("BTC", "c", Decimal(100), 10))
    with caplog.at_level(logging.INFO, logger="defi_oracle_vault.pricing"), pytest.raises(
        InsufficientValidSources
    ) as ei:
        agg.evaluate_at(cfg, 10)
    by_type = {type(x) for x in ei.value.rejected}
    assert QuoteExpired in by_type
    assert QuoteNonPositive in by_type
    # 同一批次的判定依据与输入价格都进日志
    assert "evaluate_at asset=BTC" in caplog.text
    assert "ts=10" in caplog.text
    assert "-1" in caplog.text


def test_history_outlier_rejected_with_other_sources_valid():
    book = PriceBook(retention_seconds=1000.0)
    agg = PriceAggregator(book)
    cfg = make_cfg(sources=("a", "b", "c"), min_valid_sources=2)
    book.ingest(Quote("BTC", "a", Decimal(100), 10))
    book.ingest(Quote("BTC", "b", Decimal(101), 10))
    book.ingest(Quote("BTC", "c", Decimal(200), 10))
    r = agg.evaluate_at(cfg, 10)
    assert set(r.used_sources) == {"a", "b"}
    assert any(
        rec.source == "c" and rec.reason_code == "outlier" for rec in r.rejected
    )


def test_history_unconfigured_source_recorded_but_excluded():
    book = PriceBook(retention_seconds=1000.0)
    agg = PriceAggregator(book)
    cfg = make_cfg(sources=("a", "b"), min_valid_sources=2)
    book.ingest(Quote("BTC", "a", Decimal(100), 10))
    book.ingest(Quote("BTC", "b", Decimal(101), 10))
    book.ingest(Quote("BTC", "rogue", Decimal(9999), 10))
    r = agg.evaluate_at(cfg, 10)
    assert r.price == Decimal("100.5")
    assert ("rogue", "unconfigured") in {
        (x.source, x.reason_code) for x in r.rejected
    }


@pytest.mark.parametrize("bad_ts", [float("nan"), float("inf"), float("-inf")])
def test_invalid_history_ts_rejected_without_crash(bad_ts):
    book = PriceBook(retention_seconds=1000.0)
    agg = PriceAggregator(book)
    cfg = make_cfg()
    with pytest.raises(InvalidHistoryQuery):
        agg.evaluate_at(cfg, bad_ts)


def test_history_uses_latest_quote_not_exceeding_query_ts():
    # 同一来源多条报价：只用不晚于查询时点的最新一条
    book = PriceBook(retention_seconds=1000.0)
    agg = PriceAggregator(book)
    cfg = make_cfg(sources=("a", "b"), min_valid_sources=2)
    for ts, price in [(0, "100"), (10, "110"), (20, "120")]:
        book.ingest(Quote("BTC", "a", Decimal(price), ts))
        book.ingest(Quote("BTC", "b", Decimal(price), ts))
    assert agg.evaluate_at(cfg, 15).price == Decimal(110)
    assert agg.evaluate_at(cfg, 20).price == Decimal(120)
    assert agg.evaluate_at(cfg, 0).price == Decimal(100)


def test_duplicate_ts_same_source_is_idempotent_override():
    book = PriceBook(retention_seconds=1000.0, max_quotes_per_source=5)
    agg = PriceAggregator(book)
    cfg = make_cfg(sources=("a",), min_valid_sources=1)
    book.ingest(Quote("BTC", "a", Decimal(100), 10))
    book.ingest(Quote("BTC", "a", Decimal(105), 10))  # 同时点覆盖
    assert book.retained_count("BTC", "a") == 1
    assert agg.evaluate_at(cfg, 10).price == Decimal(105)
