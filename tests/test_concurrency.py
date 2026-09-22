"""并发存取的端到端一致性：经过估值、账本、风控全链路。"""

import logging
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from defi_oracle_vault.errors import (
    AssetUnavailable,
    CircuitOpen,
    InsufficientShares,
)
from defi_oracle_vault.models import AssetConfig
from defi_oracle_vault.vault import VaultService

D = Decimal


@pytest.fixture
def svc():
    s = VaultService(share_scale=18, cash_scale=8)
    s.configure(
        AssetConfig(
            asset="BTC",
            sources=("s1", "s2"),
            min_valid_sources=2,
            max_age_seconds=3600.0,
            volatility_window_seconds=3600.0,
            volatility_threshold=D("0.50"),  # 测试中价格恒定，不会触发
        )
    )
    for i in range(2):
        s.ingest_quote("BTC", f"s{i + 1}", D("100"), 0)
    return s


def test_concurrent_mixed_operations_keep_books_consistent(svc, caplog):
    users = [f"u{i}" for i in range(10)]
    for u in users:
        svc.deposit(u, "BTC", "100", now=0)

    with caplog.at_level(logging.INFO, logger="defi_oracle_vault.pricing"):

        def worker(u: str):
            for _ in range(300):
                svc.deposit(u, "BTC", "1", now=0)
                try:
                    svc.withdraw(u, "BTC", "1", now=0)
                except InsufficientShares:  # 理论上不发生，保险
                    pass

        with ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(worker, users))

    audit = svc.audit("BTC")
    assert audit["shares_conserved"]
    assert audit["total_shares"] == D("1000")
    assert all(svc.ledger.balance_of("BTC", u) == D("100") for u in users)
    # 并发过程中仍打印价格输入与判定依据（NAV=100，无来源被拒）
    assert "evaluate asset=BTC" in caplog.text
    assert "used=('s1', 's2')" in caplog.text


def test_concurrent_competing_withdrawals_no_overdraft(svc):
    svc.deposit("bank", "BTC", "100", now=0)

    def grab(u: str) -> int:
        got = 0
        while True:
            try:
                svc.withdraw("bank", "BTC", "0.1", now=0)
                svc.deposit(u, "BTC", "0.1", now=0)
                got += 1
            except InsufficientShares:
                return got

    with ThreadPoolExecutor(max_workers=16) as pool:
        counts = list(pool.map(grab, [f"u{i}" for i in range(16)]))
    assert sum(counts) == 1000  # 100 / 0.1
    assert svc.ledger.balance_of("BTC", "bank") == 0
    assert svc.audit("BTC")["total_shares"] == D("100")


def test_concurrent_deposit_value_conservation(svc):
    users = [f"u{i}" for i in range(8)]
    value_each = D("123.456789")

    def worker(u: str):
        svc.deposit_value(u, "BTC", value_each, now=0)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, users))

    nav = D("100")
    audit = svc.audit("BTC")
    user_cash = sum(
        (svc.cash_balance(u, "BTC") for u in users), D("0")
    )
    # 全部投入价值 = 总份额*NAV + 用户找零 + 现金尘埃
    assert (
        audit["total_shares"] * nav + user_cash + audit["cash_dust"]
        == value_each * 8
    )
    assert audit["shares_conserved"]
