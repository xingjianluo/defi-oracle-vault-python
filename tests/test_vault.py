"""服务层：NAV 估值、份额/价值存取、找零、守恒、失败语义可区分。"""

import logging
from decimal import Decimal

import pytest

from defi_oracle_vault.errors import (
    AssetUnavailable,
    CircuitOpen,
    InsufficientShares,
    InvalidAmount,
    RecoveryConditionsNotMet,
    UnauthorizedRecovery,
    UnknownAsset,
)
from defi_oracle_vault.models import AssetConfig, CircuitState
from defi_oracle_vault.vault import VaultService

D = Decimal


@pytest.fixture
def svc():
    s = VaultService(share_scale=18, cash_scale=8, operators=frozenset({"admin"}))
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


def test_unknown_asset_and_no_price(svc):
    with pytest.raises(UnknownAsset):
        svc.deposit("u", "NOPE", "1", now=0)
    with pytest.raises(AssetUnavailable):
        svc.nav("BTC", now=0)


def test_deposit_value_change_is_conserved(svc, caplog):
    feed(svc, {"s1": "100", "s2": "101"}, 0)
    with caplog.at_level(logging.INFO, logger="defi_oracle_vault.vault"):
        r = svc.deposit_value("alice", "BTC", "250", now=0)
    nav = r.price  # median(100,101)=100.5
    assert nav == D("100.5")
    cash = svc.cash_balance("alice", "BTC")
    dust = svc.audit("BTC")["cash_dust"]
    # 价值严格守恒：shares*NAV + 用户现金 + 现金尘埃 == 250
    assert r.shares * nav + cash + dust == D("250")
    assert r.shares == D("2.487562189054726368")
    assert "deposit_value" in caplog.text and "100.5" in caplog.text


def test_deposit_value_dust_amount_rejected(svc):
    feed(svc, {"s1": "1000000", "s2": "1000001"}, 0)
    # 1e-13 / 1e6 = 1e-19 < 1e-18（最小份额精度），拒绝且无副作用
    with pytest.raises(InvalidAmount):
        svc.deposit_value("alice", "BTC", "0.0000000000001", now=0)
    assert svc.ledger.total_shares("BTC") == 0


def test_share_deposit_withdraw_and_valuation(svc):
    feed(svc, {"s1": "100", "s2": "100"}, 0)
    svc.deposit("alice", "BTC", "2", now=0)
    svc.deposit("bob", "BTC", "3", now=0)
    v = svc.valuation("alice", "BTC", now=0)
    assert v["shares"] == D("2") and v["nav"] == D("100") and v["value"] == D("200")
    r = svc.withdraw("alice", "BTC", "0.5", now=0)
    assert r.balance_after == D("1.5")
    audit = svc.audit("BTC")
    assert audit["shares_conserved"] and audit["total_shares"] == D("4.5")


def test_insufficient_shares_distinct_from_unavailable(svc):
    feed(svc, {"s1": "100", "s2": "100"}, 0)
    svc.deposit("alice", "BTC", "1", now=0)
    with pytest.raises(InsufficientShares):
        svc.withdraw("alice", "BTC", "2", now=0)
    # 失败无副作用
    assert svc.ledger.balance_of("BTC", "alice") == D("1")
    # 价格过期后再提：资产不可用，与份额不足是不同异常
    with pytest.raises(AssetUnavailable):
        svc.withdraw("alice", "BTC", "0.5", now=100)


def test_stale_prices_never_used(svc):
    feed(svc, {"s1": "100", "s2": "100"}, 0)
    assert svc.nav("BTC", now=0) == D("100")
    with pytest.raises(AssetUnavailable):
        svc.nav("BTC", now=31)
    # 单源新鲜仍不足 min_valid_sources
    feed(svc, {"s1": "110"}, 31)
    with pytest.raises(AssetUnavailable):
        svc.nav("BTC", now=31)


def test_circuit_blocks_deposit_but_not_withdraw_when_price_valid(svc):
    feed(svc, {"s1": "100", "s2": "101"}, 0)
    svc.deposit("alice", "BTC", "1", now=0)
    # 触发波动熔断
    feed(svc, {"s1": "120", "s2": "121"}, 90)
    svc.price_of("BTC", now=90)
    assert svc.risk.status("BTC").state is CircuitState.OPEN
    with pytest.raises(CircuitOpen):
        svc.deposit("bob", "BTC", "1", now=90)
    with pytest.raises(CircuitOpen):
        svc.deposit_value("bob", "BTC", "100", now=90)
    # 价格仍有效时，提取不受熔断阻止
    r = svc.withdraw("alice", "BTC", "0.25", now=90)
    assert r.balance_after == D("0.75")


def test_open_plus_unavailable_withdraw_is_asset_unavailable(svc):
    feed(svc, {"s1": "100", "s2": "101"}, 0)
    svc.deposit("alice", "BTC", "1", now=0)
    feed(svc, {"s1": "120", "s2": "121"}, 90)
    svc.price_of("BTC", now=90)  # OPEN
    # 随后报价过期
    with pytest.raises(AssetUnavailable):
        svc.withdraw("alice", "BTC", "0.25", now=200)


def test_prolonged_unavailable_trip_and_controlled_recovery(svc):
    feed(svc, {"s1": "100", "s2": "101"}, 0)
    svc.deposit("alice", "BTC", "1", now=0)
    # 报价在 t=30 前有效；t=40 起不可用，grace=40 内仍 CLOSED
    assert svc.risk.status("BTC").state is CircuitState.CLOSED
    for t in (40, 60, 80):
        try:
            svc.price_of("BTC", now=t)
        except AssetUnavailable:
            pass
    assert svc.risk.status("BTC").state is CircuitState.CLOSED
    try:
        svc.price_of("BTC", now=81)
    except AssetUnavailable:
        pass
    st = svc.risk.status("BTC")
    assert st.state is CircuitState.OPEN
    assert st.evidence.code == "prolonged_unavailable"
    # 越权恢复
    feed(svc, {"s1": "100", "s2": "101"}, 300)
    with pytest.raises(UnauthorizedRecovery):
        svc.recover_circuit("mallory", "BTC", now=300)
    # 授权恢复成功（窗口内仅一个新样本，波动无法构成）
    out = svc.recover_circuit("admin", "BTC", now=300)
    assert out.state is CircuitState.CLOSED


def test_recovery_denied_while_volatile(svc):
    feed(svc, {"s1": "100", "s2": "101"}, 0)
    svc.price_of("BTC", now=0)
    feed(svc, {"s1": "120", "s2": "121"}, 50)
    svc.price_of("BTC", now=50)  # OPEN, 窗口内波动大
    with pytest.raises(RecoveryConditionsNotMet):
        svc.recover_circuit("admin", "BTC", now=50)


def test_cash_withdraw():
    # share_scale=6：floor(250/100.5)=2.487562，找零为可见现金
    s = VaultService(share_scale=6, cash_scale=8)
    s.configure(
        AssetConfig(asset="BTC", sources=("s1", "s2"), min_valid_sources=2)
    )
    feed(s, {"s1": "100", "s2": "101"}, 0)
    r = s.deposit_value("alice", "BTC", "250", now=0)
    assert r.shares == D("2.487562")
    cash = s.cash_balance("alice", "BTC")
    assert cash > 0
    assert r.shares * r.price + cash + s.audit("BTC")["cash_dust"] == D("250")
    with pytest.raises(InsufficientShares):
        s.withdraw_cash("alice", "BTC", cash + D("1"))
    assert s.withdraw_cash("alice", "BTC", cash) == 0


def test_random_operation_sequence_conservation(svc):
    """任意操作序列后：份额守恒 + 每笔价值守恒恒成立。"""
    import random

    rng = random.Random(42)
    feed(svc, {"s1": "100", "s2": "100"}, 0)
    users = [f"u{i}" for i in range(5)]
    for u in users:
        svc.deposit(u, "BTC", "10", now=0)

    t = 0
    expected_total = D("50")
    expected_holdings: dict[str, Decimal] = {u: D("10") for u in users}
    for _ in range(300):
        t += 1
        price = D("100") + D(rng.randint(-3, 3))  # ±3% 不触发熔断
        feed(svc, {"s1": str(price), "s2": str(price)}, t)
        u = rng.choice(users)
        bal = svc.ledger.balance_of("BTC", u)
        if bal > 0 and rng.random() < 0.5:
            qty = min(bal, D("1"))
            svc.withdraw(u, "BTC", qty, now=t)
            expected_total -= qty
            expected_holdings[u] -= qty
        else:
            svc.deposit(u, "BTC", "1", now=t)
            expected_total += D("1")
            expected_holdings[u] += D("1")

    audit = svc.audit("BTC")
    assert audit["shares_conserved"]
    assert audit["total_shares"] == expected_total
    assert audit["user_shares_sum"] == sum(expected_holdings.values(), D("0"))
