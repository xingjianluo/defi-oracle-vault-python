"""价格聚合测试：覆盖缺失/过期/非正数/离群/来源不足。"""

from decimal import Decimal

import pytest

from app.errors import AssetNotConfiguredError
from app.models import AssetConfig, PriceQuote, RejectReason, SourceConfig
from app.price import PriceAggregator


def make_cfg(**kw):
    base = {
        "symbol": "BTC",
        "sources": (SourceConfig("s1"), SourceConfig("s2"), SourceConfig("s3")),
        "min_valid_sources": 2,
        "max_price_age_seconds": 60.0,
        "outlier_deviation": Decimal("0.05"),
    }
    base.update(kw)
    return AssetConfig(**base)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def q(source, price, ts, now):
    return PriceQuote(source=source, price=price, timestamp=ts, observed_at=now)


def test_happy_path_weighted_average(caplog):
    clk = Clock(1000.0)
    agg = PriceAggregator(make_cfg(), clock=clk)
    res = agg.ingest([
        q("s1", Decimal("100"), 999.0, 1000.0),
        q("s2", Decimal("102"), 999.0, 1000.0),
        q("s3", Decimal("101"), 999.0, 1000.0),
    ])
    assert res.usable and res.price == Decimal("101.00000000")
    assert set(res.accepted) == {"s1", "s2", "s3"}
    # 日志中应包含价格输入与判定
    assert "聚合开始" in caplog.text and "判定可用" in caplog.text


def test_missing_source_is_rejected():
    agg = PriceAggregator(make_cfg(), clock=Clock(1000.0))
    res = agg.ingest([
        q("s1", Decimal("100"), 999.0, 1000.0),
        q("s2", Decimal("100"), 999.0, 1000.0),
        # s3 缺失，但仍有 2 个有效 -> 可用
    ])
    assert res.usable
    rej = {r.source: r.reason for r in res.rejections}
    assert rej == {"s3": RejectReason.MISSING}


def test_stale_source_is_rejected():
    agg = PriceAggregator(make_cfg(), clock=Clock(1000.0))
    res = agg.ingest([
        q("s1", Decimal("100"), 900.0, 1000.0),   # age=100 > 60
        q("s2", Decimal("101"), 999.0, 1000.0),
        q("s3", Decimal("99"), 999.0, 1000.0),
    ])
    assert res.usable
    rej = [r for r in res.rejections if r.source == "s1"]
    assert rej and rej[0].reason == RejectReason.STALE


def test_non_positive_is_rejected():
    agg = PriceAggregator(make_cfg(), clock=Clock(1000.0))
    res = agg.ingest([
        q("s1", Decimal("0"), 999.0, 1000.0),
        q("s2", Decimal("-5"), 999.0, 1000.0),
        q("s3", Decimal("100"), 999.0, 1000.0),
    ])
    assert not res.usable and res.failure_code == "P005"
    reasons = {r.source: r.reason for r in res.rejections}
    assert reasons["s1"] == RejectReason.NON_POSITIVE
    assert reasons["s2"] == RejectReason.NON_POSITIVE


def test_outlier_is_rejected():
    agg = PriceAggregator(make_cfg(), clock=Clock(1000.0))
    res = agg.ingest([
        q("s1", Decimal("100"), 999.0, 1000.0),
        q("s2", Decimal("100"), 999.0, 1000.0),
        q("s3", Decimal("200"), 999.0, 1000.0),  # 偏离 100%
    ])
    assert res.usable
    rej = [r for r in res.rejections if r.source == "s3"]
    assert rej and rej[0].reason == RejectReason.OUTLIER
    assert res.price == Decimal("100.00000000")


def test_two_sources_diverge_both_rejected_and_unusable():
    cfg = make_cfg(sources=(SourceConfig("s1"), SourceConfig("s2")), min_valid_sources=2)
    agg = PriceAggregator(cfg, clock=Clock(1000.0))
    res = agg.ingest([
        q("s1", Decimal("100"), 999.0, 1000.0),
        q("s2", Decimal("200"), 999.0, 1000.0),
    ])
    assert not res.usable and res.failure_code == "P005"
    assert {r.reason for r in res.rejections} == {RejectReason.OUTLIER}


def test_insufficient_sources_no_fallback():
    cfg = make_cfg(sources=(SourceConfig("s1"), SourceConfig("s2"), SourceConfig("s3")))
    agg = PriceAggregator(cfg, clock=Clock(1000.0))
    res = agg.ingest([
        q("s1", Decimal("100"), 900.0, 1000.0),  # stale
        q("s2", Decimal("0"), 999.0, 1000.0),    # non-positive
        # s3 missing
    ])
    assert not res.usable and res.price is None and res.failure_code == "P005"
    reasons = {r.source: r.reason for r in res.rejections}
    assert set(reasons.values()) == {
        RejectReason.STALE,
        RejectReason.NON_POSITIVE,
        RejectReason.MISSING,
    }


def test_unconfigured_source_rejected():
    agg = PriceAggregator(make_cfg(), clock=Clock(1000.0))
    res = agg.ingest([
        q("rogue", Decimal("100"), 999.0, 1000.0),
        q("s1", Decimal("100"), 999.0, 1000.0),
        q("s2", Decimal("100"), 999.0, 1000.0),
    ])
    assert res.usable
    rej = [r for r in res.rejections if r.source == "rogue"]
    assert rej and rej[0].reason == RejectReason.UNCONFIGURED


def test_require_asset():
    agg = PriceAggregator(make_cfg(), clock=Clock(1000.0))
    with pytest.raises(AssetNotConfiguredError):
        agg.require_asset("ETH")
