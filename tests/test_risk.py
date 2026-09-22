"""熔断与恢复测试。"""

from decimal import Decimal

import pytest

from app.errors import (
    CircuitBreakerNotTrippedError,
    RecoveryConditionsNotMetError,
    UnauthorizedRecoveryError,
)
from app.models import (
    AggregationResult,
    AssetConfig,
    BreakerState,
    SourceConfig,
)
from app.risk import CircuitBreaker


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def make_cfg(**kw):
    base = {
        "symbol": "BTC",
        "sources": (SourceConfig("s1"), SourceConfig("s2")),
        "volatility_threshold": Decimal("0.10"),
        "observation_window_seconds": 300.0,
        "unavailable_grace_seconds": 120.0,
        "recovery_required_valid_observations": 3,
    }
    base.update(kw)
    return AssetConfig(**base)


def usable(price, at):
    return AggregationResult(
        symbol="BTC", usable=True, price=Decimal(price),
        median=Decimal(price), accepted={"s1": Decimal(price)},
        rejections=(), observed_at=at,
    )


def unusable(at, code="P005"):
    return AggregationResult(
        symbol="BTC", usable=False, price=None, median=None,
        accepted={}, rejections=(), observed_at=at, failure_code=code,
    )


def make(cfg=None, operators=frozenset({"risk_admin"})):
    clk = Clock(1000.0)
    return CircuitBreaker(cfg or make_cfg(), operators, clock=clk), clk


def test_volatility_trips_breaker():
    cb, _clk = make()
    cb.observe(usable("100", 1000.0))
    assert cb.state == BreakerState.CLOSED
    trig = cb.observe(usable("120", 1100.0))  # +20% > 10%
    assert trig is not None and trig.kind == "volatility"
    assert cb.is_open
    assert "120" in trig.detail and "100" in trig.detail  # 可解释依据


def test_swing_within_threshold_stays_closed():
    cb, _clk = make()
    cb.observe(usable("100", 1000.0))
    cb.observe(usable("105", 1100.0))
    cb.observe(usable("109", 1200.0))
    assert not cb.is_open


def test_prolonged_unavailable_trips_breaker():
    cb, _clk = make()
    cb.observe(unusable(1000.0))
    assert not cb.is_open
    cb.observe(unusable(1060.0))  # 60s, within grace
    assert not cb.is_open
    trig = cb.observe(unusable(1121.0))  # 121s > 120s grace
    assert trig is not None and trig.kind == "prolonged_unavailable"
    assert cb.is_open


def test_recovery_happy_path():
    cb, _clk = make()
    cb.observe(usable("100", 1000.0))
    cb.observe(usable("120", 1100.0))  # trips
    assert cb.is_open
    # 恢复后稳定的 3 次估值（窗口内波动 1%）
    cb.observe(usable("100", 1200.0))
    cb.observe(usable("100.5", 1300.0))
    with pytest.raises(RecoveryConditionsNotMetError):
        cb.recover("risk_admin")  # 只有 2 次
    cb.observe(usable("101", 1400.0))
    rec = cb.recover("risk_admin")
    assert not cb.is_open
    assert rec.operator == "risk_admin"
    assert rec.previous_trigger.kind == "volatility"


def test_unauthorized_recovery_rejected():
    cb, _clk = make()
    cb.observe(usable("100", 1000.0))
    cb.observe(usable("120", 1100.0))
    with pytest.raises(UnauthorizedRecoveryError):
        cb.recover("attacker")
    assert cb.is_open  # 状态不变


def test_recover_when_closed_rejected():
    cb, _clk = make()
    with pytest.raises(CircuitBreakerNotTrippedError):
        cb.recover("risk_admin")


def test_recovery_blocked_when_volatility_persists():
    cb, _clk = make()
    cb.observe(usable("100", 1000.0))
    cb.observe(usable("120", 1100.0))  # trips
    # 3 次有效但彼此波动仍超阈值
    cb.observe(usable("100", 1200.0))
    cb.observe(usable("105", 1300.0))
    cb.observe(usable("115", 1400.0))
    with pytest.raises(RecoveryConditionsNotMetError):
        cb.recover("risk_admin")
    assert cb.is_open


def test_unavailable_counter_resets_on_valid():
    cfg = make_cfg(unavailable_grace_seconds=120.0)
    cb, _clk = make(cfg)
    cb.observe(unusable(1000.0))
    cb.observe(unusable(1100.0))   # 100s
    cb.observe(usable("100", 1110.0))  # reset
    cb.observe(unusable(1120.0))
    cb.observe(unusable(1230.0))   # only 110s since 1120
    assert not cb.is_open
