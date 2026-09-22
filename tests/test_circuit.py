"""风控熔断：波动触发、长期不可用触发、受控恢复（鉴权 + 条件）。"""

import logging
from decimal import Decimal

import pytest

from defi_oracle_vault.circuit import RiskMonitor
from defi_oracle_vault.errors import (
    CircuitOpen,
    RecoveryConditionsNotMet,
    UnauthorizedRecovery,
)
from defi_oracle_vault.models import (
    AggregationResult,
    AssetConfig,
    CircuitState,
)

D = Decimal


def cfg(**kw):
    defaults = dict(
        asset="BTC",
        sources=("a", "b"),
        min_valid_sources=2,
        volatility_window_seconds=100.0,
        volatility_threshold=D("0.10"),
        unavailable_grace_seconds=40.0,
    )
    defaults.update(kw)
    return AssetConfig(**defaults)


def res(price, ts, asset="BTC"):
    return AggregationResult(asset, D(price), ("a", "b"), (), ts)


def test_volatility_trip_keeps_evidence(caplog):
    rm = RiskMonitor(operators=frozenset({"admin"}))
    rm.register(cfg())
    with caplog.at_level(logging.INFO, logger="defi_oracle_vault.circuit"):
        rm.record_available(res("100", 0), cfg(), 0)
        rm.record_available(res("105", 50), cfg(), 50)
        assert rm.status("BTC").state is CircuitState.CLOSED
        rm.record_available(res("120", 90), cfg(), 90)  # (120-100)/100=0.2
    st = rm.status("BTC")
    assert st.state is CircuitState.OPEN
    assert st.evidence.code == "volatility"
    assert "0.2" in st.evidence.detail
    assert "risk.OPEN" in caplog.text
    with pytest.raises(CircuitOpen):
        rm.ensure_not_open("BTC")


def test_prolonged_unavailability_trip():
    rm = RiskMonitor(operators=frozenset({"admin"}))
    rm.register(cfg())
    rm.record_unavailable(cfg(), 0)
    rm.record_unavailable(cfg(), 40)  # 恰好宽限，不触发（严格大于）
    assert rm.status("BTC").state is CircuitState.CLOSED
    rm.record_unavailable(cfg(), 40.001)
    st = rm.status("BTC")
    assert st.state is CircuitState.OPEN
    assert st.evidence.code == "prolonged_unavailable"


def test_no_silent_recovery_and_unauthorized():
    rm = RiskMonitor(operators=frozenset({"admin"}))
    rm.register(cfg())
    rm.record_available(res("100", 0), cfg(), 0)
    rm.record_available(res("120", 90), cfg(), 90)
    assert rm.status("BTC").state is CircuitState.OPEN
    # 即使价格重新可用，也不会自动恢复：record_available 不改 OPEN->CLOSED
    rm.record_available(res("120", 95), cfg(), 95)
    assert rm.status("BTC").state is CircuitState.OPEN
    with pytest.raises(UnauthorizedRecovery):
        rm.recover("mallory", cfg(), res("120", 95), 95)
    # 权限正确但波动仍超阈值 -> 拒绝并列明原因
    with pytest.raises(RecoveryConditionsNotMet) as ei:
        rm.recover("admin", cfg(), res("120", 95), 95)
    assert ei.value.reasons  # 非空、逐条
    assert rm.status("BTC").state is CircuitState.OPEN


def test_recovery_requires_valid_price_and_calm_window():
    rm = RiskMonitor(operators=frozenset({"admin"}))
    c = cfg()
    rm.register(c)
    # prolonged_unavailable 打开
    rm.record_unavailable(c, 0)
    rm.record_unavailable(c, 41)
    assert rm.status("BTC").state is CircuitState.OPEN
    # 价格仍不可用不能恢复
    with pytest.raises(RecoveryConditionsNotMet):
        rm.recover("admin", c, None, 42)
    # 窗口滑过旧样本、价格重新有效且波动平复 -> 人工恢复成功
    out = rm.recover("admin", c, res("100", 200), 200)
    assert out.state is CircuitState.CLOSED
    # 历史证据仍保留，可解释
    assert rm.history("BTC")[0].code == "prolonged_unavailable"


def test_failed_recovery_does_not_pollute_samples():
    rm = RiskMonitor(operators=frozenset({"admin"}))
    c = cfg()
    rm.register(c)
    rm.record_available(res("100", 0), c, 0)
    rm.record_available(res("120", 90), c, 90)
    samples_before = list(rm._samples["BTC"])
    for _ in range(3):
        with pytest.raises(RecoveryConditionsNotMet):
            rm.recover("admin", c, res("121", 95), 95)
    assert rm._samples["BTC"] == samples_before
