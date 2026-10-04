"""本地资产库演示（无需外部服务，直接 python scripts/demo.py）。

故事线：
1. 配置 BTC 三个价格源与风控参数；
2. 摄入报价，其中包含过期/非正/离群/缺失等异常，观察可区分拒绝原因；
3. 按价值存入、按份额提取，演示找零现金与守恒对账；
4. 价格剧烈波动触发熔断，存款被拒、提取在价格有效时放行；
5. 越权恢复被拒、条件不满足被拒、满足后由 admin 受控恢复；
6. 历史回看：过去某时点的聚合价/持仓回放/对账（只读，不泄漏未来）。
"""

from __future__ import annotations

import logging
from decimal import Decimal as D

from defi_oracle_vault.errors import (
    AssetUnavailable,
    CircuitOpen,
    InsufficientShares,
    InsufficientValidSources,
    InvalidTimestamp,
    RecoveryConditionsNotMet,
    UnauthorizedRecovery,
)
from defi_oracle_vault.models import AssetConfig
from defi_oracle_vault.vault import VaultService


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")

    svc = VaultService(share_scale=18, cash_scale=8, operators=frozenset({"admin"}))
    svc.configure(
        AssetConfig(
            asset="BTC",
            sources=("binance", "chainlink", "coinbase"),
            min_valid_sources=2,
            max_age_seconds=30.0,
            outlier_deviation=D("0.05"),
            volatility_window_seconds=100.0,
            volatility_threshold=D("0.10"),
            unavailable_grace_seconds=40.0,
        )
    )

    def feed(t: float, prices: dict[str, str]) -> None:
        for source, value in prices.items():
            svc.ingest_quote("BTC", source, D(value), t)

    banner("1) 异常报价的可区分拒绝（缺失/非正/离群/过期）")
    feed(0, {"binance": "100", "chainlink": "-10"})  # coinbase 缺失; chainlink 非正
    try:
        svc.price_of("BTC", now=0)
    except AssetUnavailable as e:
        print("判定不可用:", e)
    feed(0, {"chainlink": "101", "coinbase": "200"})  # coinbase 离群
    r = svc.price_of("BTC", now=0)
    print(f"聚合价={r.price} 采用={r.used_sources}")
    print("拒绝明细:", [(x.source, x.reason_code, x.detail) for x in r.rejected])

    banner("2) 估值存取与守恒")
    receipt = svc.deposit_value("alice", "BTC", "250", now=0)
    print(f"按价值存入250 -> 份额={receipt.shares} NAV={receipt.price}")
    print("找零现金:", svc.cash_balance("alice", "BTC"))
    svc.deposit("bob", "BTC", "3", now=0)
    try:
        svc.withdraw("bob", "BTC", "999", now=0)
    except InsufficientShares as e:
        print("超额提取被拒:", e)
    print("对账:", svc.audit("BTC"))

    banner("3) 波动熔断：存款被拒，价格有效时提取放行")
    feed(90, {"binance": "120", "chainlink": "121", "coinbase": "120.5"})
    svc.price_of("BTC", now=90)  # (121-100)/100=0.21 触发
    print("熔断状态:", svc.risk.status("BTC").state.value,
          "依据:", svc.risk.status("BTC").evidence.detail)
    try:
        svc.deposit("carol", "BTC", "1", now=90)
    except CircuitOpen as e:
        print("存款被拒:", e)
    out_receipt = svc.withdraw("alice", "BTC", "0.5", now=90)
    print("提取成功，余额:", out_receipt.balance_after)

    banner("4) 受控恢复：越权/条件不满足/满足")
    try:
        svc.recover_circuit("mallory", "BTC", now=90)
    except UnauthorizedRecovery as e:
        print("越权被拒:", e)
    try:
        svc.recover_circuit("admin", "BTC", now=90)
    except RecoveryConditionsNotMet as e:
        print("条件不满足:", e.reasons)
    feed(200, {"binance": "120", "chainlink": "121", "coinbase": "120.5"})
    st = svc.recover_circuit("admin", "BTC", now=200)
    print("恢复成功，当前状态:", st.state.value)

    banner("5) 长期不可用熔断与不静默恢复")
    try:
        svc.price_of("BTC", now=300)
    except AssetUnavailable:
        pass
    for t in (320, 341):
        try:
            svc.price_of("BTC", now=t)
        except AssetUnavailable:
            pass
    st = svc.risk.status("BTC")
    print("熔断状态:", st.state.value, "依据:", st.evidence)
    print("历史触发记录:", [(e.code, e.opened_ts) for e in svc.risk.history("BTC")])
    feed(400, {"binance": "120", "chainlink": "121"})
    st = svc.recover_circuit("admin", "BTC", now=400)
    print("人工恢复后:", st.state.value)

    banner("6) 历史回看：当时的价格/持仓/对账（只读，不泄漏未来）")
    r = svc.price_of_at("BTC", 0, now=400)
    print(f"ts=0 的聚合价={r.price} 采用={r.used_sources}（后来的 120 不会倒灌）")
    print("当时拒绝明细:", [(x.source, x.reason_code) for x in r.rejected])
    v = svc.position_at("alice", "BTC", 0, now=400)
    print(f"alice 在 ts=0: 份额={v['shares']} NAV={v['nav']} 市值={v['value']}")
    v = svc.position_at("carol", "BTC", 0, now=400)
    print(f"carol 在 ts=0: has_position={v['has_position']}（当时还没建仓，不硬凑数字）")
    rec = svc.reconcile_position("alice", "BTC", 0, now=400)
    print("对账: 当时份额 + 之后净变动 =",
          rec["shares_at"], "+", rec["net_change_after"],
          "== 当前", rec["current_shares"], "->", rec["matches"])
    try:
        svc.price_of_at("BTC", 999999, now=400)
    except InvalidTimestamp as e:
        print("未来时点被拒:", e)


if __name__ == "__main__":
    main()
