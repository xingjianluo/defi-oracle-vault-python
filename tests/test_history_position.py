"""历史持仓回放：只读、可重复、无持仓与资产不可用可区分、与当前持仓对账。"""

import logging
from decimal import Decimal as D

import pytest

from defi_oracle_vault.errors import (
    HistoryOutOfRetention,
    InsufficientValidSources,
    InvalidHistoryQuery,
    NoPositionAtTime,
)
from defi_oracle_vault.models import AssetConfig, CircuitState
from defi_oracle_vault.vault import VaultService


@pytest.fixture
def svc():
    s = VaultService(share_scale=18, cash_scale=8, operators=frozenset({"admin"}))
    s.configure(
        AssetConfig(
            asset="BTC",
            sources=("s1", "s2", "s3"),
            min_valid_sources=2,
            max_age_seconds=20.0,
            outlier_deviation=D("0.05"),
            volatility_window_seconds=1000.0,
            volatility_threshold=D("0.9"),
            unavailable_grace_seconds=1000.0,
        )
    )
    return s


def feed(svc, prices, t, asset="BTC"):
    for source, value in prices.items():
        svc.ingest_quote(asset, source, D(value), t)


def test_historical_position_value_uses_price_at_that_time(svc):
    feed(svc, {"s1": "100", "s2": "100"}, 0)
    svc.deposit("alice", "BTC", "10", now=0)
    feed(svc, {"s1": "200", "s2": "200"}, 50)
    svc.deposit("alice", "BTC", "5", now=50)
    # t=0 的回放必须用当时价格 100 与当时份额 10，不泄漏未来
    p0 = svc.historical_position("alice", "BTC", 0)
    assert p0.shares == D(10) and p0.price == D(100) and p0.value == D(1000)
    # 反复查询结果一致
    assert svc.historical_position("alice", "BTC", 0) == p0
    p50 = svc.historical_position("alice", "BTC", 50)
    assert p50.shares == D(15) and p50.price == D(200) and p50.value == D(3000)


def test_no_position_distinct_from_unavailable(svc):
    feed(svc, {"s1": "100", "s2": "100"}, 0)
    svc.deposit("alice", "BTC", "1", now=0)
    # bob 从未持仓 -> NoPositionAtTime（即使价格当时有效）
    with pytest.raises(NoPositionAtTime):
        svc.historical_position("bob", "BTC", 0)
    # alice 有持仓，但查询时点报价过期/来源不足 -> 不可用异常透传，而非硬凑数字
    with pytest.raises(InsufficientValidSources):
        svc.historical_position("alice", "BTC", 30)
    # alice 在持仓发生之前的时点也属于"无持仓"
    with pytest.raises(NoPositionAtTime):
        svc.historical_position("alice", "BTC", -1)


def test_no_price_observation_distinct_for_position(svc):
    feed(svc, {"s1": "100", "s2": "100"}, 100)
    svc.deposit("alice", "BTC", "1", now=100)
    # 留存窗内、当时确实还没有任何报价 -> NoPriceObservation（不是无持仓，
    # 因为查询时点早于其首笔持仓时应先报 NoPositionAtTime）
    with pytest.raises(NoPositionAtTime):
        svc.historical_position("alice", "BTC", 50)
    # 构造"已持仓、随后某来源报价失效"的场景：b 在 t=5 报非正价，
    # 当时有效来源只剩 a（min_valid_sources=2），明确不可用
    s2 = VaultService()
    s2.configure(AssetConfig(asset="ETH", sources=("a", "b"), min_valid_sources=2))
    s2.ingest_quote("ETH", "a", D(10), 0)
    s2.ingest_quote("ETH", "b", D(10), 0)
    s2.deposit("alice", "ETH", "2", now=0)
    s2.ingest_quote("ETH", "a", D(11), 5)
    s2.ingest_quote("ETH", "b", D(-1), 5)
    with pytest.raises(InsufficientValidSources):
        s2.historical_position("alice", "ETH", 5)


def test_history_query_is_read_only(svc):
    feed(svc, {"s1": "100", "s2": "101"}, 0)
    svc.deposit("alice", "BTC", "10", now=0)
    # 制造一次熔断 OPEN（波动）
    feed(svc, {"s1": "500", "s2": "501"}, 100)
    svc.price_of("BTC", now=100)
    assert svc.risk.status("BTC").state is CircuitState.OPEN
    samples_before = len(svc.risk._samples["BTC"])
    # 熔断 OPEN 下，历史回放仍可读，且不改变状态/样本/账本
    p = svc.historical_position("alice", "BTC", 0)
    assert p.price == D("100.5")
    assert svc.risk.status("BTC").state is CircuitState.OPEN
    assert len(svc.risk._samples["BTC"]) == samples_before
    assert svc.ledger.balance_of("BTC", "alice") == D(10)
    # 历史聚合价查询同样只读（无持仓也能查）
    h = svc.historical_price("BTC", 0)
    assert h.price == D("100.5")
    assert len(svc.risk._samples["BTC"]) == samples_before


def test_reconcile_historical_to_current_with_explained_delta(svc, caplog):
    feed(svc, {"s1": "100", "s2": "100"}, 0)
    svc.deposit("alice", "BTC", "10", now=0)
    feed(svc, {"s1": "100", "s2": "100"}, 10)
    svc.withdraw("alice", "BTC", "3", now=10)
    feed(svc, {"s1": "100", "s2": "100"}, 20)
    svc.deposit("alice", "BTC", "2", now=20)
    with caplog.at_level(logging.INFO, logger="defi_oracle_vault.vault"):
        rec = svc.reconcile_position("alice", "BTC", 0)
    # current(9) == historical(10) + net(-3+2)
    assert rec.matches is True
    assert rec.historical_shares == D(10)
    assert rec.current_shares == D(9)
    assert rec.net_delta == D(-1)
    assert [e.kind for e in rec.events] == ["withdraw", "deposit"]
    assert [e.shares_delta for e in rec.events] == [D(-3), D(2)]
    assert "reconcile_position" in caplog.text


def test_reconcile_partial_window_and_withdraw_to_zero(svc):
    feed(svc, {"s1": "100", "s2": "100"}, 0)
    svc.deposit("alice", "BTC", "5", now=0)
    feed(svc, {"s1": "100", "s2": "100"}, 10)
    svc.withdraw("alice", "BTC", "5", now=10)  # 提光
    feed(svc, {"s1": "100", "s2": "100"}, 20)
    svc.deposit("alice", "BTC", "7", now=20)
    # t=10 时点份额为 0，但 had_position 仍成立（提光不等于从未持仓）
    p10 = svc.historical_position("alice", "BTC", 10)
    assert p10.shares == D(0) and p10.value == D(0)
    # 从 t=10 对账到当前：0 + 7 = 7
    rec = svc.reconcile_position("alice", "BTC", 10)
    assert rec.matches and rec.net_delta == D(7) and rec.current_shares == D(7)


def test_reconcile_events_respect_ts_boundary_inclusion(svc):
    feed(svc, {"s1": "100", "s2": "100"}, 0)
    svc.deposit("alice", "BTC", "1", now=0)
    feed(svc, {"s1": "100", "s2": "100"}, 5)
    svc.deposit("alice", "BTC", "2", now=5)
    # 区间 (0, 5]：包含 t=5 这笔，不包含 t=0
    rec = svc.reconcile_position("alice", "BTC", 0, current_ts=5)
    assert len(rec.events) == 1 and rec.events[0].shares_delta == D(2)
    assert rec.matches and rec.current_shares == D(3)


def test_invalid_history_ts_on_service(svc):
    feed(svc, {"s1": "100", "s2": "100"}, 0)
    svc.deposit("alice", "BTC", "1", now=0)
    for bad in (float("nan"), float("inf")):
        with pytest.raises(InvalidHistoryQuery):
            svc.historical_price("BTC", bad)
        with pytest.raises(InvalidHistoryQuery):
            svc.historical_position("alice", "BTC", bad)
        with pytest.raises(InvalidHistoryQuery):
            svc.reconcile_position("alice", "BTC", bad)
    with pytest.raises(HistoryOutOfRetention):
        svc.historical_price("BTC", -10**9)
    assert svc.risk.status("BTC").state is CircuitState.CLOSED  # 非法请求不污染状态
