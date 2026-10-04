"""并发：写入与历史回放同时进行时，已封口历史结论稳定、只读、不拖慢正常存取。

关键语义（"不泄漏未来"在并发下的准确表述）：
- 历史查询 t 只使用 ts' <= t 的报价；
- 一旦写入水位越过 t（watermark >= t），t 时点即"封口"：此后无论再写入
  多少 ts > t 的新报价，对 t 的反复回放结论必须完全一致、永不改变；
- 历史读为只读，不改变熔断状态/风控样本/账本现金；
- 并发历史读不拖慢正常存取，账本始终守恒、不透支。
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as D

from defi_oracle_vault.errors import (
    HistoryOutOfRetention,
    InsufficientValidSources,
    NoPriceObservation,
)
from defi_oracle_vault.models import AssetConfig
from defi_oracle_vault.vault import VaultService


def _make_service():
    s = VaultService(
        quote_retention_seconds=1_000_000.0,
        max_quotes_per_source=1_000_000,
        share_scale=18,
        cash_scale=8,
    )
    s.configure(
        AssetConfig(
            asset="BTC",
            sources=("s1", "s2"),
            min_valid_sources=2,
            max_age_seconds=1e9,
            volatility_window_seconds=1e9,
            volatility_threshold=D(10),
        )
    )
    return s


def test_sealed_history_is_stable_under_concurrent_future_writes():
    svc = _make_service()
    # 阶段一（单线程）先把 t=0..200 的事实全部喂完，让 t=100 成为"已封口的过去"
    svc.ingest_quote("BTC", "s1", D(100), 0)
    svc.ingest_quote("BTC", "s2", D(100), 0)
    svc.deposit("alice", "BTC", "10", now=0)
    for t in range(1, 201):
        svc.ingest_quote("BTC", "s1", D(100) + D(t), float(t))
        svc.ingest_quote("BTC", "s2", D(100) + D(t), float(t))
        if t in (50, 100, 150):
            svc.deposit("alice", "BTC", "1", now=float(t))

    errors: list[Exception] = []
    freeze_prices: set[D] = set()
    freeze_positions: set[tuple] = set()

    def future_writer():
        """只写 ts>200 的新报价/存取，绝不触碰 t<=100 的过去。"""
        try:
            for t in range(201, 601):
                svc.ingest_quote("BTC", "s1", D(100) + D(t), float(t))
                svc.ingest_quote("BTC", "s2", D(100) + D(t), float(t))
                if t % 50 == 0:  # 250,300,...,600 共 8 笔
                    svc.deposit("alice", "BTC", "1", now=float(t))
        except Exception as e:  # noqa: BLE001  收集工作线程任何失败
            errors.append(e)

    def history_reader():
        try:
            for _ in range(400):
                hp = svc.historical_price("BTC", 100.0)
                assert hp.price == D(200), hp
                assert set(hp.used_sources) == {"s1", "s2"}
                freeze_prices.add(hp.price)
                pos = svc.historical_position("alice", "BTC", 0)
                assert pos.shares == D(10) and pos.price == D(100)
                freeze_positions.add((pos.shares, pos.price, pos.value))
        except Exception as e:  # noqa: BLE001  收集工作线程任何失败
            errors.append(e)

    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = [pool.submit(future_writer)] + [
            pool.submit(history_reader) for _ in range(4)
        ]
        for f in futs:
            f.result()

    assert not errors, errors
    # 已封口的 t=100：并发写入再多多，结论也只有一个，绝不前后矛盾
    assert freeze_prices == {D(200)}, freeze_prices
    assert freeze_positions == {(D(10), D(100), D(1000))}, freeze_positions
    audit = svc.audit("BTC")
    assert audit["shares_conserved"]
    # 初始 10 + 建设期 3 笔 + 未来期 8 笔 = 21
    assert audit["total_shares"] == D(21)
    # 历史对账：从 t=100 到当前，净变化逐笔可解释且对平
    rec = svc.reconcile_position("alice", "BTC", 100)
    assert rec.matches
    assert rec.historical_shares == D(12)  # 10 + 50 + 100
    assert rec.current_shares == D(21)
    assert rec.net_delta == D(9)  # 150 一笔 + 未来 8 笔


def test_history_reads_do_not_corrupt_live_access_or_books():
    svc = _make_service()
    svc.ingest_quote("BTC", "s1", D(100), 0)
    svc.ingest_quote("BTC", "s2", D(100), 0)
    for u in ("u1", "u2", "u3"):
        svc.deposit(u, "BTC", "100", now=0)

    stop = threading.Event()
    errors: list[Exception] = []

    def live_writer():
        """单一写线程成对推进两源；同时做等额存取（抵消，余额守恒）。"""
        t = 0
        try:
            while not stop.is_set():
                t += 1
                price = D(100 + (t % 50))
                # 成对摄入在服务锁内原子化，保证实时估值始终是自洽的一对
                with svc._lock:
                    svc.ingest_quote("BTC", "s1", price, float(t))
                    svc.ingest_quote("BTC", "s2", price, float(t))
                    assert svc.nav("BTC", now=float(t)) == price
                svc.deposit("u1", "BTC", "1", now=float(t))
                svc.withdraw("u1", "BTC", "1", now=float(t))
        except Exception as e:  # noqa: BLE001  收集工作线程任何失败
            errors.append(e)

    def history_reader():
        t = 0
        try:
            while not stop.is_set():
                t += 1
                try:
                    svc.historical_price("BTC", float(t) / 2)
                except (NoPriceObservation, HistoryOutOfRetention, InsufficientValidSources):
                    pass
                rec = svc.reconcile_position("u2", "BTC", 0)  # 只读
                assert rec.matches and rec.current_shares == D(100)
        except Exception as e:  # noqa: BLE001  收集工作线程任何失败
            errors.append(e)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = [pool.submit(live_writer)] + [
            pool.submit(history_reader) for _ in range(3)
        ]
        stop.wait(0.5)
        stop.set()
        for f in futs:
            f.result()

    assert not errors, errors
    audit = svc.audit("BTC")
    assert audit["shares_conserved"]
    assert svc.ledger.balance_of("BTC", "u1") == D(100)  # 存取抵消
    assert svc.ledger.balance_of("BTC", "u2") == D(100)
