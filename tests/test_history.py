"""历史回看：保留边界、不泄漏未来、可区分失败、对账、并发一致性。"""

import logging
import threading
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from defi_oracle_vault.api import create_app
from defi_oracle_vault.errors import (
    HistoryOutOfRetention,
    InsufficientValidSources,
    InvalidTimestamp,
    LedgerHistoryPruned,
    NoPriceObservation,
    QuoteExpired,
    QuotePruned,
)
from defi_oracle_vault.models import AssetConfig, HistoryRetention, Quote
from defi_oracle_vault.pricing import PriceAggregator, PriceBook
from defi_oracle_vault.vault import VaultService

D = Decimal


def make_cfg(**kw):
    defaults = dict(
        asset="BTC",
        sources=("s1", "s2", "s3"),
        min_valid_sources=2,
        max_age_seconds=10.0,
        outlier_deviation=D("0.05"),
    )
    defaults.update(kw)
    return AssetConfig(**defaults)


def make_svc(retention=None, **kw):
    s = VaultService(
        share_scale=18,
        cash_scale=8,
        quote_retention=retention,
        **kw,
    )
    s.configure(
        AssetConfig(
            asset="BTC",
            sources=("s1", "s2", "s3"),
            min_valid_sources=2,
            max_age_seconds=30.0,
            outlier_deviation=D("0.05"),
            volatility_window_seconds=100.0,
            volatility_threshold=D("0.10"),
            unavailable_grace_seconds=40.0,
        )
    )
    return s


def feed(svc, prices: dict[str, str], t: float, asset: str = "BTC"):
    for source, value in prices.items():
        svc.ingest_quote(asset, source, D(value), t)


# ---- 保留策略与边界 ----


def test_retention_age_window_boundary():
    """年龄窗口边界含等号；被清理与从无报价可区分。"""
    book = PriceBook(HistoryRetention(max_age_seconds=100.0, max_entries_per_source=None))
    for ts in (0.0, 50.0, 100.0, 200.0):
        book.ingest(Quote("BTC", "s1", D("100"), ts))
    # 最新 ts=200，cutoff=100：保留 ts>=100 的条目（边界 100 恰好保留）
    q, pruned = book.as_of("BTC", "s1", 150.0)
    assert q is not None and q.ts == 100.0 and not pruned
    q, pruned = book.as_of("BTC", "s1", 100.0)
    assert q is not None and q.ts == 100.0
    # ts=50 的报价已被清理：pruned=True（而不是"当时无报价"）
    q, pruned = book.as_of("BTC", "s1", 50.0)
    assert q is None and pruned
    # ts 早于该来源有史以来第一条报价：当时确实没有报价
    q, pruned = book.as_of("BTC", "s1", -1.0)
    assert q is None and not pruned
    # 从未见过的来源：missing
    q, pruned = book.as_of("BTC", "sX", 999.0)
    assert q is None and not pruned


def test_retention_count_cap():
    """条数上限：超出时丢弃最旧；最新一条永远保留。"""
    book = PriceBook(HistoryRetention(max_age_seconds=None, max_entries_per_source=2))
    for ts in (0.0, 1.0, 2.0, 3.0):
        book.ingest(Quote("BTC", "s1", D("100"), ts))
    assert book.latest("BTC", "s1").ts == 3.0
    q, pruned = book.as_of("BTC", "s1", 2.0)
    assert q is not None and q.ts == 2.0
    q, pruned = book.as_of("BTC", "s1", 1.0)
    assert q is None and pruned


def test_as_of_exact_quote_timestamp():
    """查询时点恰好等于某条报价的 ts：该报价可用（含等号）。"""
    book = PriceBook(HistoryRetention(max_age_seconds=None, max_entries_per_source=None))
    book.ingest(Quote("BTC", "s1", D("42"), 10.0))
    q, pruned = book.as_of("BTC", "s1", 10.0)
    assert q is not None and q.value == D("42") and not pruned
    q, _ = book.as_of("BTC", "s1", 9.999)
    assert q is None


# ---- 历史聚合语义 ----


def test_no_future_leak(caplog):
    """更晚的报价绝不参与更早时点的判定（不泄漏未来信息）。"""
    book = PriceBook(HistoryRetention(max_age_seconds=None, max_entries_per_source=None))
    agg = PriceAggregator(book)
    cfg = make_cfg(max_age_seconds=1000.0)
    book.ingest(Quote("BTC", "s1", D("100"), 0.0))
    book.ingest(Quote("BTC", "s2", D("100"), 0.0))
    book.ingest(Quote("BTC", "s1", D("999"), 10.0))
    book.ingest(Quote("BTC", "s2", D("999"), 10.0))
    with caplog.at_level(logging.INFO, logger="defi_oracle_vault.pricing"):
        early = agg.evaluate_as_of(cfg, 5.0)
        late = agg.evaluate_as_of(cfg, 10.0)
    assert early.price == D("100")  # 不是 999，也不是两者混合
    assert late.price == D("999")
    assert "evaluate_as_of" in caplog.text and "100" in caplog.text


def test_missing_vs_pruned_distinguishable():
    """三种资产级结论可区分：当时无报价(InsufficientValidSources/missing)、
    已被清理(HistoryOutOfRetention)、从无观测(NoPriceObservation)。"""
    book = PriceBook(HistoryRetention(max_age_seconds=100.0, max_entries_per_source=None))
    agg = PriceAggregator(book)
    cfg = make_cfg(max_age_seconds=1000.0)
    # 只喂 s1、s2；s3 配置但从未报价
    book.ingest(Quote("BTC", "s1", D("100"), 0.0))
    book.ingest(Quote("BTC", "s2", D("100"), 0.0))
    book.ingest(Quote("BTC", "s1", D("101"), 500.0))
    book.ingest(Quote("BTC", "s2", D("101"), 500.0))  # ts=0 的报价被清理

    # ts=400：s1/s2 的旧报价已被清理，且早于回看边界 horizon=500
    # -> 资产级独立结论 HistoryOutOfRetention（不再混进笼统的 503）
    with pytest.raises(HistoryOutOfRetention) as eh:
        agg.evaluate_as_of(cfg, 400.0)
    out = eh.value
    assert out.horizon == 500.0
    assert out.pruned_sources == ("s1", "s2")
    assert out.available_series == 0 and out.required == 2
    by_source = {r.source: r for r in out.rejected}
    assert isinstance(by_source["s1"], QuotePruned)
    assert isinstance(by_source["s2"], QuotePruned)
    # s3 从来没报过价：在明细里仍是 missing，而不是 pruned
    assert type(by_source["s3"]).__name__ == "QuoteMissing"

    # 查询时点比所有来源的第一条报价还早（当时确实没有报价）：
    # 即使有来源数据已经被清理，也报"有效来源不足"，而不是"超出保留范围"
    # horizon 随保留推进，但最早观测 0.0 被永久记住
    with pytest.raises(InsufficientValidSources) as ei:
        agg.evaluate_as_of(cfg, -1.0)
    reasons = {r.source: type(r).__name__ for r in ei.value.rejected}
    assert reasons == {"s1": "QuoteMissing", "s2": "QuoteMissing", "s3": "QuoteMissing"}

    # 从无观测的资产：NoPriceObservation（与"曾被清理"不同）
    with pytest.raises(NoPriceObservation):
        agg.evaluate_as_of(make_cfg(asset="ETH"), 400.0)


def test_never_quoted_source_vs_pruned_in_retention_window():
    """保留范围内（ts>=horizon）混合原因导致来源不足：报 InsufficientValidSources，
    逐来源原因固定（pruned/missing/non_positive 各自归位），不会误报超出保留。"""
    book = PriceBook(HistoryRetention(max_age_seconds=100.0, max_entries_per_source=None))
    agg = PriceAggregator(book)
    cfg = make_cfg(sources=("s1", "s2", "s3", "s4"), min_valid_sources=2,
                   max_age_seconds=1000.0)
    book.ingest(Quote("BTC", "s1", D("100"), 0.0))    # 保留、有效
    book.ingest(Quote("BTC", "s2", D("-5"), 0.0))     # 保留、非正
    book.ingest(Quote("BTC", "s3", D("100"), 0.0))
    book.ingest(Quote("BTC", "s3", D("101"), 500.0))  # ts=0 被清理 -> ts=10 时 pruned
    # s4 从未报价

    # horizon=0.0（s1、s2 保留起点），ts=10 >= horizon：不属于"超出保留范围"；
    # 但有效来源只有 s1（s2 非正、s3 已清理、s4 从没报过）-> 来源不足
    with pytest.raises(InsufficientValidSources) as ei:
        agg.evaluate_as_of(cfg, 10.0)
    reasons = {r.source: type(r).__name__ for r in ei.value.rejected}
    assert reasons["s2"] == "QuoteNonPositive"
    assert reasons["s3"] == "QuotePruned"
    assert reasons["s4"] == "QuoteMissing"

    # 同一来源同时踩中多个拒绝条件时，同点反复查询结论完全一致（可复现）
    again = {}
    with pytest.raises(InsufficientValidSources) as ei2:
        agg.evaluate_as_of(cfg, 10.0)
    again = {r.source: type(r).__name__ for r in ei2.value.rejected}
    assert again == reasons

    # 查询时点早于所有来源的第一条报价：全部 missing，仍不是"超出保留范围"
    with pytest.raises(InsufficientValidSources) as ei:
        agg.evaluate_as_of(
            make_cfg(sources=("s1", "s2", "s3"), min_valid_sources=2,
                     max_age_seconds=1000.0),
            -1.0,
        )
    reasons = {r.source: type(r).__name__ for r in ei.value.rejected}
    assert set(reasons.values()) == {"QuoteMissing"}


def test_unavailable_at_query_time_not_substituted():
    """查询时点全部过期 -> 明确不可用，不拿更早/更晚价格顶替。"""
    book = PriceBook(HistoryRetention(max_age_seconds=None, max_entries_per_source=None))
    agg = PriceAggregator(book)
    cfg = make_cfg(max_age_seconds=10.0)
    book.ingest(Quote("BTC", "s1", D("100"), 0.0))
    book.ingest(Quote("BTC", "s2", D("100"), 0.0))
    with pytest.raises(InsufficientValidSources) as ei:
        agg.evaluate_as_of(cfg, 20.0)  # 两条报价年龄 20 > 10
    by_source = {r.source: r for r in ei.value.rejected}
    assert isinstance(by_source["s1"], QuoteExpired)
    assert isinstance(by_source["s2"], QuoteExpired)


def test_multiple_rejection_conditions_precedence_reproducible():
    """同一来源同时踩中多个拒绝条件时，按管线顺序报第一个，结论可复现。"""
    book = PriceBook(HistoryRetention(max_age_seconds=None, max_entries_per_source=None))
    agg = PriceAggregator(book)
    cfg = make_cfg(sources=("s1", "s2"), min_valid_sources=2, max_age_seconds=10.0)
    book.ingest(Quote("BTC", "s1", D("-5"), 0.0))  # 非正；且在 ts=100 时也过期
    book.ingest(Quote("BTC", "s2", D("100"), 0.0))

    def reasons_at(ts):
        with pytest.raises(InsufficientValidSources) as ei:
            agg.evaluate_as_of(cfg, ts)
        return {r.source: type(r).__name__ for r in ei.value.rejected}

    # ts=0：s1 非正（非正检查先于离群）
    first = reasons_at(0.0)
    assert first == {"s1": "QuoteNonPositive"}
    # ts=100：s1 同时过期+非正 -> 报 expired（管线顺序确定）
    second = reasons_at(100.0)
    assert second == {"s1": "QuoteExpired", "s2": "QuoteExpired"}
    # 同时点反复查询结论一致
    assert reasons_at(100.0) == second


def test_evaluate_as_of_readonly_and_repeatable():
    """历史评估只读且可重复：不改动价格簿，也不影响当前聚合。"""
    book = PriceBook(HistoryRetention(max_age_seconds=None, max_entries_per_source=None))
    agg = PriceAggregator(book)
    cfg = make_cfg(max_age_seconds=1000.0)
    book.ingest(Quote("BTC", "s1", D("100"), 0.0))
    book.ingest(Quote("BTC", "s2", D("102"), 0.0))
    r1 = agg.evaluate_as_of(cfg, 5.0)
    r2 = agg.evaluate_as_of(cfg, 5.0)
    assert r1 == r2 and r1.price == D("101")
    assert book.latest("BTC", "s1").value == D("100")  # 簿子未被改动


def test_asset_horizon_boundary_inclusive_with_count_cap():
    """条数上限下的资产回看边界：正好等于边界可用；早于边界才超出保留。"""
    # 每来源只留 2 条；min_valid_sources=2
    book = PriceBook(HistoryRetention(max_age_seconds=None, max_entries_per_source=2))
    agg = PriceAggregator(book)
    cfg = make_cfg(sources=("s1", "s2"), min_valid_sources=2, max_age_seconds=1000.0)
    for t in (0.0, 1.0, 2.0):
        book.ingest(Quote("BTC", "s1", D("100"), t))
        book.ingest(Quote("BTC", "s2", D("100"), t))
    # 保留 [1,2]，回看边界 horizon=1
    assert book.history_horizon("BTC", ("s1", "s2"), 2) == 1.0

    # ts 恰好等于边界（==某条报价 ts）：正常用上那条报价
    assert agg.evaluate_as_of(cfg, 1.0).price == D("100")
    # 早于边界一个极小微元：确定判为超出保留范围
    with pytest.raises(HistoryOutOfRetention) as eh:
        agg.evaluate_as_of(cfg, 0.999999999)
    assert eh.value.horizon == 1.0
    # 已经被清理的 ts=0 也超出保留（而不是 missing）
    with pytest.raises(HistoryOutOfRetention):
        agg.evaluate_as_of(cfg, 0.0)


def test_horizon_advances_as_retention_rolls():
    """持续摄入下回看边界按保留规则确定推进；每个时点的结论都可由 horizon 解释，
    且不随查询次数摇摆。年龄窗边界含等号（newest-ts==max_age 仍保留）。"""
    book = PriceBook(HistoryRetention(max_age_seconds=10.0, max_entries_per_source=None))
    agg = PriceAggregator(book)
    cfg = make_cfg(sources=("s1", "s2"), min_valid_sources=2, max_age_seconds=1000.0)

    book.ingest(Quote("BTC", "s1", D("100"), 0.0))
    book.ingest(Quote("BTC", "s2", D("100"), 0.0))
    assert book.history_horizon("BTC", ("s1", "s2"), 2) == 0.0
    # 首次喂价之前：当时确实没有报价（missing -> 来源不足），不是超出保留
    with pytest.raises(InsufficientValidSources):
        agg.evaluate_as_of(cfg, -0.001)
    assert agg.evaluate_as_of(cfg, 0.0).price == D("100")

    # 喂 t=10：10-0==10 含等号，t=0 报价仍保留，horizon 不变
    book.ingest(Quote("BTC", "s1", D("101"), 10.0))
    book.ingest(Quote("BTC", "s2", D("101"), 10.0))
    assert book.history_horizon("BTC", ("s1", "s2"), 2) == 0.0
    assert agg.evaluate_as_of(cfg, 0.0).price == D("100")

    # 喂 t=20：t=0 超出年龄窗被清，horizon 推进到 10.0
    book.ingest(Quote("BTC", "s1", D("102"), 20.0))
    book.ingest(Quote("BTC", "s2", D("102"), 20.0))
    assert book.history_horizon("BTC", ("s1", "s2"), 2) == 10.0
    # 边界上（恰好等于仍保留的那条报价 ts）正常使用
    assert agg.evaluate_as_of(cfg, 10.0).price == D("101")
    # 早于边界：超出保留范围；反复查询结论恒定
    for _ in range(3):
        with pytest.raises(HistoryOutOfRetention) as eh:
            agg.evaluate_as_of(cfg, 9.999)
        assert eh.value.horizon == 10.0


def test_out_of_retention_is_deterministic_with_illegal_values(caplog):
    """异常输入（非数值/NaN）打不崩；同一时点反复查询结论、原因、边界恒定。"""
    book = PriceBook(HistoryRetention(max_age_seconds=10.0, max_entries_per_source=None))
    agg = PriceAggregator(book)
    cfg = make_cfg(sources=("s1", "s2"), min_valid_sources=2, max_age_seconds=1000.0)
    for t in (0.0, 100.0):
        book.ingest(Quote("BTC", "s1", D("100"), t))
        book.ingest(Quote("BTC", "s2", D("100"), t))

    outcomes = set()
    with caplog.at_level(logging.WARNING, logger="defi_oracle_vault.pricing"):
        for _ in range(5):
            try:
                agg.evaluate_as_of(cfg, 50.0)
            except HistoryOutOfRetention as e:
                outcomes.add(
                    (type(e).__name__, e.horizon, e.pruned_sources,
                     tuple((r.source, type(r).__name__) for r in e.rejected))
                )
    assert len(outcomes) == 1  # 结论完全固定、可复现
    assert "OUT_OF_RETENTION" in caplog.text and "horizon=100.0" in caplog.text
    # 非法/异常时间点（NaN/+inf）不打崩、结论固定：原语一律 (None, False)，
    # 不依赖 NaN 比较语义；时间合法性由服务层 _check_query_ts 明确拒绝(422)
    for bad in (float("nan"), float("inf"), float("-inf")):
        assert book.as_of("BTC", "s1", bad) == (None, False)


# ---- 服务层：历史价格与持仓回放 ----


def test_price_history_horizon_and_out_of_retention_service():
    """服务层：回看边界可探知；早于边界的三类历史查询统一抛
    HistoryOutOfRetention（与"当时没报价"503 类错误、非法时点 422 均可区分）。"""
    svc = make_svc(HistoryRetention(max_age_seconds=5.0, max_entries_per_source=None))
    # 尚未喂够来源：边界不可知（None）
    assert svc.price_history_horizon("BTC") is None
    feed(svc, {"s1": "100", "s2": "100"}, 0.0)
    assert svc.price_history_horizon("BTC") == 0.0
    feed(svc, {"s1": "110", "s2": "110"}, 10.0)  # ts=0 被清理，边界推进到 10
    assert svc.price_history_horizon("BTC") == 10.0

    # 边界上：可用；早于边界：HistoryOutOfRetention，带确定的边界依据
    assert svc.price_of_at("BTC", 10.0, now=1000.0).price == D("110")
    with pytest.raises(HistoryOutOfRetention) as eh:
        svc.price_of_at("BTC", 7.0, now=1000.0)
    assert eh.value.horizon == 10.0 and eh.value.available_series == 0
    assert eh.value.pruned_sources == ("s1", "s2")

    # 持仓回放同样抛 HistoryOutOfRetention，且账本/现金/熔断均未被触碰
    svc.deposit("alice", "BTC", "3", now=10.0)
    with pytest.raises(HistoryOutOfRetention):
        svc.position_at("alice", "BTC", 7.0, now=1000.0)
    assert svc.ledger.balance_of("BTC", "alice") == D("3")
    assert svc.risk.status("BTC").state.value == "CLOSED"


def test_out_of_retention_query_is_readonly():
    """超出保留范围的失败查询同样只读：不改熔断、不录样本、不动账本现金。"""
    svc = make_svc(HistoryRetention(max_age_seconds=5.0, max_entries_per_source=None))
    feed(svc, {"s1": "100", "s2": "100"}, 0.0)
    feed(svc, {"s1": "110", "s2": "110"}, 10.0)
    svc.deposit("alice", "BTC", "2", now=10.0)

    samples_before = len(svc.risk._samples.get("BTC", ()))
    audit_before = svc.audit("BTC")
    for _ in range(10):
        with pytest.raises(HistoryOutOfRetention):
            svc.price_of_at("BTC", 3.0, now=1000.0)
        with pytest.raises(HistoryOutOfRetention):
            svc.position_at("alice", "BTC", 3.0, now=1000.0)
    samples_after = len(svc.risk._samples.get("BTC", ()))
    assert samples_after == samples_before
    assert svc.risk.status("BTC").state.value == "CLOSED"
    assert svc.audit("BTC") == audit_before  # 账本与现金守恒快照不变


def test_price_of_at_does_not_touch_risk():
    """历史查询不录观察样本、不改熔断状态。"""
    svc = make_svc(HistoryRetention(max_age_seconds=None, max_entries_per_source=None))
    feed(svc, {"s1": "100", "s2": "100"}, 0.0)
    before = svc.risk.status("BTC")
    for _ in range(5):
        r = svc.price_of_at("BTC", 0.0, now=1000.0)
        assert r.price == D("100")
    after = svc.risk.status("BTC")
    assert after.state == before.state
    assert after.last_good_ts is None  # 当前估值路径从未运行，样本未被污染
    assert svc.risk.history("BTC") == []


def test_invalid_query_ts_distinguished():
    """非法时间点：非有限/负数/未来，各自抛 InvalidTimestamp 且不打崩。"""
    svc = make_svc(HistoryRetention(max_age_seconds=None, max_entries_per_source=None))
    feed(svc, {"s1": "100", "s2": "100"}, 0.0)
    for bad in (float("nan"), float("inf"), -1.0):
        with pytest.raises(InvalidTimestamp):
            svc.price_of_at("BTC", bad, now=1000.0)
    with pytest.raises(InvalidTimestamp):
        svc.price_of_at("BTC", 1001.0, now=1000.0)  # 未来
    with pytest.raises(InvalidTimestamp):
        svc.position_at("alice", "BTC", "not-a-number", now=1000.0)
    # 服务仍可用
    assert svc.price_of_at("BTC", 0.0, now=1000.0).price == D("100")


def test_position_at_replay_and_no_position(caplog):
    """持仓回放：份额、当时 NAV、市值；未建仓与余额为 0 可区分。"""
    svc = make_svc(HistoryRetention(max_age_seconds=None, max_entries_per_source=None))
    feed(svc, {"s1": "100", "s2": "100"}, 0.0)
    feed(svc, {"s1": "110", "s2": "110"}, 10.0)
    svc.deposit("alice", "BTC", "2", now=1.0)
    svc.deposit("bob", "BTC", "3", now=5.0)
    svc.withdraw("alice", "BTC", "1", now=8.0)

    with caplog.at_level(logging.INFO, logger="defi_oracle_vault.vault"):
        v = svc.position_at("alice", "BTC", 7.0, now=1000.0)
    assert v["shares"] == D("2") and v["has_position"] is True
    assert v["nav"] == D("100") and v["value"] == D("200")  # 用当时的价，不用 110
    v = svc.position_at("alice", "BTC", 10.0, now=1000.0)
    assert v["shares"] == D("1") and v["nav"] == D("110") and v["value"] == D("110")
    # bob 在 ts=2 还没建仓：has_position=False，不硬凑数字
    v = svc.position_at("bob", "BTC", 2.0, now=1000.0)
    assert v["shares"] == D("0") and v["has_position"] is False and v["value"] == D("0")
    # carol 从未建仓
    v = svc.position_at("carol", "BTC", 10.0, now=1000.0)
    assert v["has_position"] is False
    assert "position_at" in caplog.text and "alice" in caplog.text


def test_position_unavailable_vs_no_position_distinguishable():
    """资产当时不可用（抛错）与用户当时无持仓（正常返回标志位）是两种结论。"""
    svc = make_svc(HistoryRetention(max_age_seconds=None, max_entries_per_source=None))
    feed(svc, {"s1": "100", "s2": "100"}, 0.0)
    svc.deposit("alice", "BTC", "2", now=1.0)
    # ts=500 时报价早已过期（max_age=30）：资产当时不可用
    with pytest.raises(InsufficientValidSources):
        svc.position_at("alice", "BTC", 500.0, now=1000.0)
    # 同一时刻换个没建仓的用户：仍是资产不可用，而不是"无持仓"
    with pytest.raises(InsufficientValidSources):
        svc.position_at("carol", "BTC", 500.0, now=1000.0)
    # 资产可用但用户无持仓：正常返回 has_position=False
    v = svc.position_at("carol", "BTC", 1.0, now=1000.0)
    assert v["has_position"] is False


def test_reconcile_position_against_current():
    """对账：当前份额 == 历史时点份额 + 之后净变动，差额可解释。"""
    svc = make_svc(HistoryRetention(max_age_seconds=None, max_entries_per_source=None))
    feed(svc, {"s1": "100", "s2": "100"}, 0.0)
    svc.deposit("alice", "BTC", "2", now=1.0)
    svc.deposit("alice", "BTC", "3", now=2.0)
    svc.withdraw("alice", "BTC", "1", now=3.0)
    svc.deposit("bob", "BTC", "7", now=4.0)  # 其他用户不影响 alice 的对账

    r = svc.reconcile_position("alice", "BTC", 1.0, now=1000.0)
    assert r["shares_at"] == D("2")
    assert r["deposited_after"] == D("3") and r["withdrawn_after"] == D("1")
    assert r["events_after"] == 2
    assert r["current_shares"] == D("4") == r["expected_current"]
    assert r["matches"] is True

    r = svc.reconcile_position("alice", "BTC", 0.0, now=1000.0)
    assert r["shares_at"] == D("0") and r["net_change_after"] == D("4")
    assert r["matches"] is True


def test_ledger_compaction_pruned_vs_no_position():
    """账本事件压实：边界之前抛 LedgerHistoryPruned，与"当时无持仓"可区分。"""
    svc = make_svc(
        HistoryRetention(max_age_seconds=None, max_entries_per_source=None),
        ledger_history_max_events=3,
    )
    feed(svc, {"s1": "100", "s2": "100"}, 0.0)
    svc.deposit("alice", "BTC", "1", now=0.0)
    svc.deposit("alice", "BTC", "1", now=1.0)
    svc.withdraw("alice", "BTC", "1", now=2.0)
    svc.deposit("alice", "BTC", "1", now=3.0)
    svc.deposit("alice", "BTC", "1", now=4.0)  # 5 事件 > 上限 3，最旧 2 条被压实

    # 边界(horizon=1)之前：无法精确回放
    with pytest.raises(LedgerHistoryPruned):
        svc.ledger.balance_at("BTC", "alice", 0.0)
    # 边界及之后：精确回放且与当前对账一致
    assert svc.ledger.balance_at("BTC", "alice", 1.0) == D("2")
    r = svc.reconcile_position("alice", "BTC", 1.0, now=1000.0)
    assert r["matches"] is True and r["current_shares"] == D("3")
    # 被压实用户曾建仓的事实不丢
    assert svc.ledger.has_position_at("BTC", "alice", 1.0) is True
    # 从未建仓的用户：has_position=False（不是 Pruned 错误）
    assert svc.ledger.has_position_at("BTC", "carol", 4.0) is False


# ---- 并发一致性 ----


def test_concurrent_writes_and_historical_queries():
    """持续写新报价/存取的同时跑历史查询：同一时点结果恒定、对账始终成立。"""
    svc = make_svc(HistoryRetention(max_age_seconds=None, max_entries_per_source=None))
    feed(svc, {"s1": "100", "s2": "102"}, 0.0)
    svc.deposit("alice", "BTC", "5", now=0.0)

    errors: list[BaseException] = []
    historical_prices: set[str] = set()
    historical_shares: set[str] = set()
    reconcile_ok: list[bool] = []
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            i += 1
            t = 1000.0 + i
            feed(svc, {"s1": str(100 + i % 7), "s2": str(101 + i % 7)}, t)
            try:
                svc.deposit("alice", "BTC", "1", now=t)
            except Exception as e:  # noqa: BLE001
                errors.append(e)
                return

    def reader():
        while not stop.is_set():
            try:
                p = svc.price_of_at("BTC", 0.0, now=500.0)
                historical_prices.add(str(p.price))
                v = svc.position_at("alice", "BTC", 0.0, now=500.0)
                historical_shares.add(str(v["shares"]))
                r = svc.reconcile_position("alice", "BTC", 0.0, now=10_000_000.0)
                reconcile_ok.append(r["matches"])
            except Exception as e:  # noqa: BLE001
                errors.append(e)
                return

    threads = [threading.Thread(target=writer)] + [
        threading.Thread(target=reader) for _ in range(4)
    ]
    for t in threads:
        t.start()
    threading.Event().wait(1.5)
    stop.set()
    for t in threads:
        t.join()

    assert not errors
    # 同一历史时点的查询结果不因并发写入而变化
    assert historical_prices == {"101"}
    assert historical_shares == {"5"}
    # 写入持续发生时，对账恒等式每次都成立
    assert reconcile_ok and all(reconcile_ok)


def test_concurrent_out_of_retention_is_consistent():
    """持续写价（带滚动保留）+ 并发历史查询：同一早于边界的时点，
    要么始终是 HistoryOutOfRetention，要么在边界回退瞬间给出确定结论，
    不允许出现异常类型/边界依据在多个结果间随机摇摆；且只读、不拖垮存取。"""
    # 年龄窗 100s，写入从 ts=1000 开始；查询点 ts=0：
    # 第一次写入后 0 即被清理，此后 horizon 只会 >=1000，ts=0 恒属超出保留
    svc = make_svc(HistoryRetention(max_age_seconds=100.0, max_entries_per_source=None))
    feed(svc, {"s1": "100", "s2": "100"}, 0.0)
    svc.deposit("alice", "BTC", "5", now=0.0)
    # 主线程先完成第一次滚动写入：ts=0 此刻已被清理、horizon>=1001，
    # 之后并发查询 ts=0 必然且始终是"超出保留范围"，消除启动竞态
    feed(svc, {"s1": "101", "s2": "101"}, 1001.0)
    assert svc.price_history_horizon("BTC") >= 1001.0

    outcomes: set[tuple] = set()
    errors: list[BaseException] = []
    deposits_done: list[int] = []
    stop = threading.Event()

    def writer():
        i = 1
        try:
            while not stop.is_set():
                i += 1
                t = 1000.0 + i
                feed(svc, {"s1": str(100 + i % 5), "s2": str(100 + i % 5)}, t)
                svc.deposit("alice", "BTC", "1", now=t)  # 实时路径不被历史读拖慢
                deposits_done.append(i)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def reader():
        try:
            while not stop.is_set():
                try:
                    svc.price_of_at("BTC", 0.0, now=10_000_000.0)
                    outcomes.add(("ok",))
                except HistoryOutOfRetention as e:
                    outcomes.add(
                        ("out_of_retention", e.horizon, e.pruned_sources)
                    )
                except NoPriceObservation:
                    outcomes.add(("no_observation",))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=writer)] + [
        threading.Thread(target=reader) for _ in range(4)
    ]
    for t in threads:
        t.start()
    threading.Event().wait(1.0)
    stop.set()
    for t in threads:
        t.join()

    assert not errors
    assert deposits_done and len(deposits_done) > 10  # 实时写入持续成功
    # ts=0 已永久处于边界之前：只能观察到单一结论（410 类），
    # 且 pruned_sources 固定；绝不可能混入成功或"从无观测"
    assert outcomes and all(o[0] == "out_of_retention" for o in outcomes)
    assert {o[2] for o in outcomes} == {("s1", "s2")}
    # 边界只能随写入单调向前，不回摆
    horizons = sorted({o[1] for o in outcomes})
    assert horizons == sorted(set(horizons)) and horizons[0] >= 1000.0
    # 历史读完全不影响实时聚合与账本：每笔存入都精确入账，守恒成立
    assert svc.ledger.balance_of("BTC", "alice") == D(5 + len(deposits_done))
    assert svc.audit("BTC")["shares_conserved"] is True


# ---- HTTP 层 ----


@pytest.fixture
def client():
    svc = VaultService(
        share_scale=6,
        operators=frozenset({"admin"}),
        quote_retention=HistoryRetention(max_age_seconds=5.0, max_entries_per_source=None),
    )
    return TestClient(create_app(svc)), svc


def _configure(c):
    r = c.post(
        "/assets/BTC/configure",
        json={
            "sources": ["s1", "s2"],
            "min_valid_sources": 2,
            "max_age_seconds": 30.0,
            "outlier_deviation": "0.05",
        },
    )
    assert r.status_code == 200, r.text


def _quote(c, source, value, ts, asset="BTC"):
    r = c.post(
        f"/assets/{asset}/quotes",
        json={"source": source, "value": value, "ts": ts},
    )
    assert r.status_code == 200, r.text


def test_history_endpoints(client):
    c, _ = client
    _configure(c)
    _quote(c, "s1", "100", 0.0)
    _quote(c, "s2", "100", 0.0)
    _quote(c, "s1", "110", 10.0)  # 保留窗 5s：ts=0 的报价被清理
    _quote(c, "s2", "110", 10.0)

    # 历史价格：ts=10 用当时的报价（恰好等于报价 ts，正常使用）
    r = c.get("/assets/BTC/price/history", params={"ts": 10.0})
    assert r.status_code == 200 and r.json()["price"] == "110"

    # 保留边界"含等号"在单元测试 test_retention_age_window_boundary 中
    # 直接覆盖（查询 ts==保留条目 ts 必然可用）。
    # ts=4：两来源 ts=0 的旧报价均已被清理，查询早于回看边界(horizon=10)
    # -> 独立的 410 / HistoryOutOfRetention，与 503 的"当时来源不足"可区分
    r = c.get("/assets/BTC/price/history", params={"ts": 4.0})
    assert r.status_code == 410
    body = r.json()
    assert body["error_code"] == "HistoryOutOfRetention"
    assert body["horizon"] == 10.0 and body["horizon_inclusive"] is True
    assert body["ts"] == 4.0 and body["required"] == 2
    assert body["retained_available"] == 0
    assert body["pruned_sources"] == ["s1", "s2"]
    assert {x["reason"] for x in body["rejected"]} == {"QuotePruned"}

    # 对比：同一资产，"当时还没开始报价"(missing) 与 "报价已被清理"(pruned)
    # 在 HTTP 层也必须可区分。新增 ETH：两来源都从 ts=20 才开始有报价
    r = c.post(
        "/assets/ETH/configure",
        json={"sources": ["e1", "e2"], "min_valid_sources": 2, "max_age_seconds": 30},
    )
    assert r.status_code == 200
    _quote(c, "e1", "100", 20.0, asset="ETH")
    # ts=10：比该资产第一条报价还早 -> 503，来源原因 missing（不是 410）
    r = c.get("/assets/ETH/price/history", params={"ts": 10.0})
    assert r.status_code == 503
    body = r.json()
    assert body["error_code"] == "InsufficientValidSources"
    assert {x["reason"] for x in body["rejected"]} == {"QuoteMissing"}
    # 补齐第二条后，ts=20 正常可用
    _quote(c, "e2", "100", 20.0, asset="ETH")
    r = c.get("/assets/ETH/price/history", params={"ts": 20.0})
    assert r.status_code == 200 and r.json()["price"] == "100"

    # 非法时间点：负数/未来/非数值 -> 422，error_code 可区分
    assert c.get("/assets/BTC/price/history", params={"ts": -1.0}).json()[
        "error_code"
    ] == "InvalidTimestamp"
    assert c.get(
        "/assets/BTC/price/history", params={"ts": 10**15}
    ).status_code == 422
    assert c.get("/assets/BTC/price/history", params={"ts": "abc"}).status_code == 422

    # 持仓回放与对账
    r = c.post("/assets/BTC/deposit", json={"user": "alice", "shares": "2", "ts": 10.0})
    assert r.status_code == 200, r.text
    r = c.get("/users/alice/positions/BTC/history", params={"ts": 10.0})
    assert r.status_code == 200
    v = r.json()
    assert v["shares"] == "2" and v["has_position"] is True and v["nav"] == "110"
    r = c.get("/users/carol/positions/BTC/history", params={"ts": 10.0})
    assert r.json()["has_position"] is False

    r = c.get("/users/alice/positions/BTC/reconcile", params={"ts": 10.0})
    assert r.status_code == 200 and r.json()["matches"] is True
