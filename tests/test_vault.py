"""门面集成测试：聚合 + 熔断 + 账本 的失败可区分性与一致性。"""

from decimal import Decimal

import pytest

from app.errors import (
    AssetNotConfiguredError,
    CircuitBreakerOpenError,
    InsufficientSharesError,
    PriceUnavailableError,
    RecoveryConditionsNotMetError,
    UnauthorizedRecoveryError,
)
from app.models import (
    AssetConfig,
    BreakerState,
    PriceQuote,
    SourceConfig,
    VaultConfig,
)
from app.vault import Vault


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def btc_cfg(**kw):
    base = {
        "symbol": "BTC",
        "sources": (SourceConfig("s1"), SourceConfig("s2")),
        "min_valid_sources": 2,
        "max_price_age_seconds": 60.0,
        "outlier_deviation": Decimal("0.05"),
        "volatility_threshold": Decimal("0.10"),
        "observation_window_seconds": 300.0,
        "unavailable_grace_seconds": 120.0,
        "recovery_required_valid_observations": 3,
    }
    base.update(kw)
    return AssetConfig(**base)


def make_vault(cfg=None, operators=frozenset({"risk_admin"})):
    clk = Clock(1000.0)
    cfg = cfg or btc_cfg()
    vc = VaultConfig(assets={"BTC": cfg}, recovery_operators=operators)
    return Vault(vc, clock=clk), clk


def quotes(prices, ts=999.0, now=1000.0, sources=("s1", "s2")):
    return [
        PriceQuote(source=s, price=Decimal(str(p)), timestamp=ts, observed_at=now)
        for s, p in zip(sources, prices)
    ]


def test_unknown_asset_p000():
    vault, _ = make_vault()
    with pytest.raises(AssetNotConfiguredError):
        vault.deposit("u", "DOGE", Decimal("1"))


def test_deposit_without_price_p006():
    vault, _ = make_vault()
    with pytest.raises(PriceUnavailableError) as ei:
        vault.deposit("alice", "BTC", Decimal("1"))
    assert ei.value.code == "P006"


def test_insufficient_sources_then_operation_p006():
    vault, _ = make_vault()
    res = vault.update_prices("BTC", quotes(["100", "0"]))  # s2 非法
    assert not res.usable and res.failure_code == "P005"
    with pytest.raises(PriceUnavailableError) as ei:
        vault.deposit("alice", "BTC", Decimal("1"))
    assert ei.value.code == "P006"


def test_deposit_withdraw_happy_path():
    vault, _clk = make_vault()
    vault.update_prices("BTC", quotes(["100", "100"]))
    r = vault.deposit("alice", "BTC", Decimal("2"))
    assert r.price == Decimal("100.00000000")
    assert vault.ledger.shares_of("alice", "BTC") == Decimal("2")

    r2 = vault.withdraw("alice", "BTC", Decimal("0.5"))
    assert r2.entry.cash_delta == Decimal("-50.00000000")
    vault.ledger.verify_conservation("BTC", Decimal("100"))


def test_stale_price_blocks_operations():
    vault, clk = make_vault()
    vault.update_prices("BTC", quotes(["100", "100"], ts=999.0, now=1000.0))
    vault.deposit("alice", "BTC", Decimal("1"))  # 新鲜，成功
    clk.t = 1200.0  # 推进 200s，超过 60s 保鲜
    with pytest.raises(PriceUnavailableError) as ei:
        vault.deposit("alice", "BTC", Decimal("1"))
    assert ei.value.code == "P006" and "过期" in str(ei.value)


def test_breaker_blocks_deposit_but_allows_withdraw():
    vault, clk = make_vault()
    vault.update_prices("BTC", quotes(["100", "100"]))
    vault.deposit("alice", "BTC", Decimal("1"))
    # 波动触发熔断
    clk.t = 1100.0
    vault.update_prices("BTC", quotes(["120", "120"], ts=1099.0, now=1100.0))
    assert vault.breaker("BTC").is_open

    with pytest.raises(CircuitBreakerOpenError) as ei:
        vault.deposit("bob", "BTC", Decimal("1"))
    assert ei.value.code == "C001"
    # 提取仍然允许（需要有效价）
    r = vault.withdraw("alice", "BTC", Decimal("0.25"))
    assert r.entry.shares == Decimal("0.25")

    # 份额不足仍是 L001，而不是熔断错误
    with pytest.raises(InsufficientSharesError) as ei2:
        vault.withdraw("alice", "BTC", Decimal("99"))
    assert ei2.value.code == "L001"


def test_prolonged_unavailable_and_controlled_recovery():
    vault, clk = make_vault()
    vault.update_prices("BTC", quotes(["100", "100"]))
    vault.deposit("alice", "BTC", Decimal("1"))

    clk.t = 1130.0
    vault.update_prices("BTC", quotes(["0", "0"], ts=1129.0, now=1130.0))
    assert not vault.breaker("BTC").is_open  # 130s since 1000? 无效从1130起
    clk.t = 1300.0
    vault.update_prices("BTC", quotes(["0", "0"], ts=1299.0, now=1300.0))
    assert vault.breaker("BTC").is_open  # 170s 不可用 > 120s grace

    # 越权恢复
    with pytest.raises(UnauthorizedRecoveryError):
        vault.recover("BTC", "mallory")
    assert vault.breaker("BTC").is_open

    # 价格恢复但只有 2 次，条件不满足
    for t, p in [(1310, "100"), (1320, "100")]:
        clk.t = float(t)
        vault.update_prices("BTC", quotes([p, p], ts=float(t) - 1, now=float(t)))
    with pytest.raises(RecoveryConditionsNotMetError):
        vault.recover("BTC", "risk_admin")

    # 第 3 次有效 + 授权 -> 恢复
    clk.t = 1330.0
    vault.update_prices("BTC", quotes(["100", "100"], ts=1329.0, now=1330.0))
    rec = vault.recover("BTC", "risk_admin")
    assert not vault.breaker("BTC").is_open
    assert rec.operator == "risk_admin"
    # 恢复后存款重新可用
    r = vault.deposit("alice", "BTC", Decimal("1"))
    assert r.breaker_state == BreakerState.CLOSED


def test_status_explains_state():
    vault, _clk = make_vault()
    vault.update_prices("BTC", quotes(["100", "100"]))
    st = vault.status("BTC")
    assert st.usable and st.last_price == Decimal("100.00000000")
    assert st.breaker == BreakerState.CLOSED
