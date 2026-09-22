"""账本：精度校验、透支拒绝、任意序列后份额守恒。"""

from decimal import Decimal

import pytest

from defi_oracle_vault.errors import (
    InsufficientShares,
    InvalidAmount,
    UnknownAsset,
)
from defi_oracle_vault.ledger import Ledger

D = Decimal


def test_register_deposit_withdraw_and_conservation():
    led = Ledger(share_scale=6)
    led.register_asset("BTC")
    led.deposit("BTC", "alice", D("10.25"))
    led.deposit("BTC", "bob", D("3.50"))
    led.withdraw("BTC", "alice", D("2.25"))
    led.deposit("BTC", "alice", D("0.75"))
    assert led.balance_of("BTC", "alice") == D("8.75")
    assert led.balance_of("BTC", "bob") == D("3.50")
    assert led.user_shares_sum("BTC") == D("12.25")
    assert led.total_shares("BTC") == D("12.25")
    assert led.dust("BTC") == 0
    assert led.user_shares_sum("BTC") + led.dust("BTC") == led.total_shares("BTC")


def test_unknown_asset_and_invalid_amounts():
    led = Ledger(share_scale=3)
    led.register_asset("ETH")
    with pytest.raises(UnknownAsset):
        led.deposit("NOPE", "u", D("1"))
    for bad in (D("0"), D("-1"), D("NaN"), D("1.0001")):
        with pytest.raises(InvalidAmount):
            led.deposit("ETH", "u", bad)


def test_insufficient_shares_is_distinct_and_atomic():
    led = Ledger()
    led.register_asset("ETH")
    led.deposit("ETH", "u", D("1.000000000000000000"))
    with pytest.raises(InsufficientShares) as ei:
        led.withdraw("ETH", "u", D("1.000000000000000001"))
    assert ei.value.available == "1.000000000000000000"
    # 失败后余额不变
    assert led.balance_of("ETH", "u") == D("1.000000000000000000")
    assert led.total_shares("ETH") == D("1.000000000000000000")


def test_concurrent_deposits_and_withdrawals_never_overdraw_or_lose_shares():
    import threading

    led = Ledger(share_scale=18)
    led.register_asset("X")
    users = [f"u{i}" for i in range(8)]
    for u in users:
        led.deposit("X", u, D("100"))

    errors: list[Exception] = []
    barrier = threading.Barrier(len(users))

    def worker(u: str):
        barrier.wait()
        for _ in range(500):
            try:
                led.withdraw("X", u, D("1"))
            except InsufficientShares:
                pass
            except Exception as e:  # pragma: no cover
                errors.append(e)
            led.deposit("X", u, D("1"))

    threads = [threading.Thread(target=worker, args=(u,)) for u in users]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    # 每用户净变化为 0（500 次取 + 500 次存），总量恒等于 800
    assert led.total_shares("X") == D("800")
    assert led.user_shares_sum("X") == D("800")
    assert all(led.balance_of("X", u) == D("100") for u in users)


def test_concurrent_competing_withdrawals_conservation():
    """多用户竞争提取同一被反复充值的资产：总量守恒，无负余额。"""
    import threading

    led = Ledger(share_scale=18)
    led.register_asset("Y")
    led.deposit("Y", "bank", D("1000"))
    errors: list[Exception] = []

    def drain(user: str):
        for _ in range(1000):
            try:
                led.withdraw("Y", "bank", D("0.5"))
                led.deposit("Y", user, D("0.5"))
            except InsufficientShares:
                return
            except Exception as e:  # pragma: no cover
                errors.append(e)

    ts = [threading.Thread(target=drain, args=(f"u{i}",)) for i in range(10)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors
    assert led.balance_of("Y", "bank") >= 0
    assert led.total_shares("Y") == D("1000")
