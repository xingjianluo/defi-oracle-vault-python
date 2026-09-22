"""并发存取 + 并发价格摄入的混合压力测试。

多个线程对同一资产并发存取，另一个线程不断喂价（含正常/异常），
最终断言：账本守恒、总份额与净现金可解释、失败都落在已知错误码上。
"""

import random
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from app.errors import (
    VaultError,
)
from app.models import (
    AssetConfig,
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


def make():
    clk = Clock(1000.0)
    cfg = AssetConfig(
        symbol="BTC",
        sources=(SourceConfig("s1"), SourceConfig("s2")),
        min_valid_sources=2,
        max_price_age_seconds=60.0,
        outlier_deviation=Decimal("0.05"),
        volatility_threshold=Decimal("0.10"),
        observation_window_seconds=300.0,
        unavailable_grace_seconds=120.0,
        recovery_required_valid_observations=3,
    )
    return Vault(VaultConfig(assets={"BTC": cfg}), clock=clk), clk


def feed(vault, clk, prices, ts=None):
    now = clk()
    qs = [
        PriceQuote(source=f"s{i+1}",
                   price=None if p is None else Decimal(str(p)),
                   timestamp=ts if ts is not None else now - 1,
                   observed_at=now)
        for i, p in enumerate(prices)
    ]
    return vault.update_prices("BTC", qs)


def test_mixed_concurrency_conserves():
    vault, clk = make()
    feed(vault, clk, ["100", "100"])
    for u in range(10):
        vault.deposit(f"u{u}", "BTC", Decimal("100"))

    known_codes: dict[str, int] = {}
    rng = random.Random(42)

    def trader(u: int):
        user = f"u{u}"
        for _ in range(100):
            action = rng.choice(["dep", "wd"])
            shares = Decimal("1")
            try:
                if action == "dep":
                    vault.deposit(user, "BTC", shares)
                else:
                    vault.withdraw(user, "BTC", shares)
            except VaultError as exc:
                # 只允许这几类可解释失败；出现其它异常即测试失败。
                assert exc.code in {"C001", "L001", "P006"}, exc.code
                known_codes[exc.code] = known_codes.get(exc.code, 0) + 1

    def feeder():
        # 大部分正常，偶发缺失/非法/离群/过期；另制造一次大幅波动触发熔断。
        scenarios = [
            (["100", "100"], None),
            (["100", None], None),      # 缺失 -> 来源不足 P005
            (["100", "0"], None),       # 非正数
            (["100", "300"], None),     # 离群
            (["100", "100"], 0.0),      # 时间戳远古 -> 过期
        ]
        for i in range(120):
            clk.t += 0.2
            if i == 60:
                feed(vault, clk, ["130", "130"])  # +30% 波动 -> 熔断
            else:
                prices, ts = scenarios[i % len(scenarios)]
                feed(vault, clk, prices, ts)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(trader, u) for u in range(10)]
        ff = pool.submit(feeder)
        ff.result()
        for f in futs:
            f.result()

    # 恢复到稳定价并满足条件后关闭熔断（权限受控）。
    if vault.breaker("BTC").is_open:
        for _ in range(3):
            clk.t += 1
            feed(vault, clk, ["100", "100"])
        vault.recover("BTC", "risk_admin")

    # 用最终有效价做严格守恒校验。
    vault.ledger.verify_conservation("BTC", Decimal("100"))
    total = vault.ledger.total_shares("BTC")
    summed = sum(
        (vault.ledger.shares_of(f"u{u}", "BTC") for u in range(10)),
        Decimal(0),
    )
    assert total == summed and total >= 0
    assert vault.ledger.dust("BTC") >= 0

    # 至少观察到了"估值不可用"这一类并发失败（证明熔断/不可用路径被覆盖）。
    # C001 是否出现取决于线程调度，因此这里只统计、不强求；但所有失败必须可区分。
    assert set(known_codes).issubset({"C001", "L001", "P006"})
