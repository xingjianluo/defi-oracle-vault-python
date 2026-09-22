"""多价格源聚合：拒绝原因可区分，不足时不回退陈旧/默认价格。"""

import logging

import pytest
from decimal import Decimal

from defi_oracle_vault.errors import (
    InsufficientValidSources,
    NoPriceObservation,
    QuoteExpired,
    QuoteMissing,
    QuoteNonPositive,
    QuoteOutlier,
)
from defi_oracle_vault.models import AssetConfig, Quote
from defi_oracle_vault.pricing import PriceAggregator, PriceBook, median_price

NOW = 1_000.0


def make_cfg(**kw):
    defaults = dict(
        asset="BTC",
        sources=("a", "b", "c"),
        min_valid_sources=2,
        max_age_seconds=10.0,
        outlier_deviation=Decimal("0.05"),
    )
    defaults.update(kw)
    return AssetConfig(**defaults)


@pytest.fixture
def book():
    return PriceBook()


@pytest.fixture
def agg(book):
    return PriceAggregator(book)


def test_no_observation_is_explicit(agg):
    with pytest.raises(NoPriceObservation):
        agg.evaluate(make_cfg(), NOW)


def test_missing_reason_is_distinct(agg, book, caplog):
    book.ingest(Quote("BTC", "a", Decimal("100"), NOW))
    with caplog.at_level(logging.INFO, logger="defi_oracle_vault.pricing"):
        with pytest.raises(InsufficientValidSources) as ei:
            agg.evaluate(make_cfg(), NOW)
    assert {type(r) for r in ei.value.rejected} == {QuoteMissing}
    assert ei.value.valid == 1 and ei.value.required == 2
    # 日志含价格输入与判定依据
    assert "evaluate asset=BTC" in caplog.text
    assert "missing" in caplog.text


def test_expired_quote_rejected(agg, book):
    book.ingest(Quote("BTC", "b", Decimal("101"), NOW - 10.001))
    with pytest.raises(InsufficientValidSources) as ei:
        agg.evaluate(make_cfg(), NOW)
    expired = [r for r in ei.value.rejected if isinstance(r, QuoteExpired)]
    assert len(expired) == 1
    r = expired[0]
    assert r.source == "b" and r.age_seconds == pytest.approx(10.001)
    # 绝不回退陈旧价：没有可用聚合结果
    assert agg.try_evaluate(make_cfg(), NOW) is None


def test_non_positive_quote_rejected(agg, book):
    book.ingest(Quote("BTC", "a", Decimal("100"), NOW))
    for bad in ("0", "-5"):
        book.ingest(Quote("BTC", "b", Decimal(bad), NOW))
        with pytest.raises(InsufficientValidSources) as ei:
            agg.evaluate(make_cfg(), NOW)
        assert isinstance(ei.value.rejected[-1], QuoteNonPositive)


def test_outlier_rejected_but_median_holds(agg, book, caplog):
    # 两源 100/101 接近；第三源 200 明显离群
    book.ingest(Quote("BTC", "a", Decimal("100"), NOW))
    book.ingest(Quote("BTC", "b", Decimal("101"), NOW))
    book.ingest(Quote("BTC", "c", Decimal("200"), NOW))
    with caplog.at_level(logging.INFO, logger="defi_oracle_vault.pricing"):
        result = agg.evaluate(make_cfg(min_valid_sources=2), NOW)
    assert set(result.used_sources) == {"a", "b"}
    assert result.price == Decimal("100.5")
    codes = {(r.source, r.reason_code) for r in result.rejected}
    assert ("c", "outlier") in codes
    assert "200" in caplog.text  # 输入价格被打印


def test_outlier_threshold_boundary_is_inclusive(agg, book):
    # 中位数 101，偏离 5/101 ≈ 0.0495 <= 0.05，不应判离群
    book.ingest(Quote("BTC", "a", Decimal("100"), NOW))
    book.ingest(Quote("BTC", "b", Decimal("101"), NOW))
    book.ingest(Quote("BTC", "c", Decimal("106"), NOW))
    result = agg.evaluate(make_cfg(min_valid_sources=2), NOW)
    assert set(result.used_sources) == {"a", "b", "c"}
    assert result.price == Decimal("101")


def test_unconfigured_source_does_not_participate(agg, book):
    book.ingest(Quote("BTC", "a", Decimal("100"), NOW))
    book.ingest(Quote("BTC", "b", Decimal("101"), NOW))
    book.ingest(Quote("BTC", "rogue", Decimal("9999"), NOW))
    result = agg.evaluate(make_cfg(), NOW)
    assert result.price == Decimal("100.5")
    assert ("rogue", "unconfigured") in {(r.source, r.reason_code) for r in result.rejected}


def test_fresh_quote_replaces_stale_no_silent_fallback(agg, book):
    cfg = make_cfg()
    book.ingest(Quote("BTC", "a", Decimal("100"), NOW))
    book.ingest(Quote("BTC", "b", Decimal("101"), NOW))
    assert agg.evaluate(cfg, NOW).price == Decimal("100.5")
    # 两源同时过期 -> 明确不可用，而不是沿用 100.5
    with pytest.raises(InsufficientValidSources):
        agg.evaluate(cfg, NOW + 11)
    # 只有一源刷新：仍因 min_valid_sources=2 不可用
    book.ingest(Quote("BTC", "a", Decimal("110"), NOW + 11))
    with pytest.raises(InsufficientValidSources):
        agg.evaluate(cfg, NOW + 11)
    # 两源都刷新后恢复，价格是新价而不是陈旧价
    book.ingest(Quote("BTC", "b", Decimal("112"), NOW + 11))
    assert agg.evaluate(cfg, NOW + 11).price == Decimal("111")


def test_median_helpers():
    assert median_price([Decimal("3")]) == Decimal("3")
    assert median_price([Decimal("1"), Decimal("3"), Decimal("2")]) == Decimal("2")
    assert median_price([Decimal("4"), Decimal("1")]) == Decimal("2.5")
