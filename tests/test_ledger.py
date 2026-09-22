"""账本测试：守恒、舍入、份额不足、并发。"""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from app.errors import InsufficientSharesError, InvalidAmountError
from app.ledger import Ledger

PREC = {"share": Decimal("0.00000001"), "cash": Decimal("0.00000001")}


def make_ledger():
    return Ledger({"BTC": {**PREC}})


def test_deposit_withdraw_basic_conservation():
    lg = make_ledger()
    lg.deposit("alice", "BTC", Decimal("1.5"), Decimal("100"))
    assert lg.shares_of("alice", "BTC") == Decimal("1.5")
    assert lg.total_shares("BTC") == Decimal("1.5")
    assert lg.net_cash("BTC") == Decimal("150.00000000")
    lg.verify_conservation("BTC", Decimal("100"))

    lg.withdraw("alice", "BTC", Decimal("0.5"), Decimal("100"))
    assert lg.shares_of("alice", "BTC") == Decimal("1.0")
    assert lg.net_cash("BTC") == Decimal("100.00000000")
    lg.verify_conservation("BTC", Decimal("100"))


def test_rounding_dust_is_display_residue():
    # 内部全精度：净现金 = 1 * 100.000000003，不丢任何资产；
    # 展示网格向下舍入，零头单独留存于 dust。
    lg = make_ledger()
    lg.deposit("alice", "BTC", Decimal("1"), Decimal("100.000000003"))
    assert lg.net_cash("BTC") == Decimal("100.000000003")
    assert lg.net_cash_rounded("BTC") == Decimal("100.00000000")
    assert lg.dust("BTC") == Decimal("0.000000003")
    assert lg.net_cash_rounded("BTC") + lg.dust("BTC") == lg.net_cash("BTC")
    lg.verify_conservation("BTC", Decimal("100.000000003"))


def test_withdraw_never_overpays():
    lg = make_ledger()
    lg.deposit("alice", "BTC", Decimal("3"), Decimal("100"))
    lg.deposit("bob", "BTC", Decimal("7"), Decimal("100"))
    # 价格变化后 alice 提取；全精度现金，绝不多发。
    lg.withdraw("alice", "BTC", Decimal("3"), Decimal("100.000000009"))
    lg.verify_conservation("BTC", Decimal("100.000000009"))
    assert lg.shares_of("alice", "BTC") == 0
    assert lg.shares_of("bob", "BTC") == Decimal("7")
    # bob 的 7 份按新价盯市
    assert lg.mark_to_market("BTC", Decimal("100.000000009")) == Decimal("700.000000063")
    # 净现金 = 存入1000 - 取出300.000000027
    assert lg.net_cash("BTC") == Decimal("699.999999973")
    assert lg.price_adjustment("BTC", Decimal("100.000000009")) == Decimal("-0.00000009")
    assert (
        lg.mark_to_market("BTC", Decimal("100.000000009"))
        + lg.price_adjustment("BTC", Decimal("100.000000009"))
        == lg.net_cash("BTC")
    )


def test_insufficient_shares_distinguishable():
    lg = make_ledger()
    lg.deposit("alice", "BTC", Decimal("1"), Decimal("100"))
    with pytest.raises(InsufficientSharesError) as ei:
        lg.withdraw("alice", "BTC", Decimal("2"), Decimal("100"))
    assert ei.value.code == "L001"
    # 失败后余额不变
    assert lg.shares_of("alice", "BTC") == Decimal("1")
    assert lg.total_shares("BTC") == Decimal("1")


def test_invalid_amounts():
    lg = make_ledger()
    for bad in (Decimal("0"), Decimal("-1"), Decimal("NaN")):
        with pytest.raises(InvalidAmountError):
            lg.deposit("alice", "BTC", bad, Decimal("100"))
    with pytest.raises(InvalidAmountError):
        lg.deposit("alice", "BTC", Decimal("1"), Decimal("0"))


def test_concurrent_deposits_and_withdrawals_conserve():
    lg = make_ledger()
    price = Decimal("123.456789")
    n_users = 20
    for u in range(n_users):
        lg.deposit(f"u{u}", "BTC", Decimal("10"), price)

    def worker(u: int):
        # 正常路径下不应出现任何异常（每人初始 10 份，等额存取）；
        # 一旦出现就会从线程池抛出，使测试失败，从而暴露并发破坏。
        for _ in range(50):
            lg.deposit(f"u{u}", "BTC", Decimal("0.1"), price)
            lg.withdraw(f"u{u}", "BTC", Decimal("0.1"), price)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(n_users)))

    # 每人回到 10 份
    for u in range(n_users):
        assert lg.shares_of(f"u{u}", "BTC") == Decimal("10.00000000")
    assert lg.total_shares("BTC") == Decimal("200.00000000")
    lg.verify_conservation("BTC", price)


def test_concurrent_overdraw_only_available_succeed():
    lg = make_ledger()
    lg.deposit("alice", "BTC", Decimal("1"), Decimal("100"))
    successes = []
    failures = []

    def withdraw_all():
        try:
            lg.withdraw("alice", "BTC", Decimal("1"), Decimal("100"))
            successes.append(1)
        except InsufficientSharesError as e:
            assert e.code == "L001"
            failures.append(1)

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lambda _: withdraw_all(), range(32)))

    assert sum(successes) == 1
    assert sum(failures) == 31
    assert lg.shares_of("alice", "BTC") == 0
    lg.verify_conservation("BTC", Decimal("100"))
