"""门面：把多源价格聚合、风控熔断、份额账本串成一套本地资产库。

一次 deposit/withdraw 的判定顺序（失败原因彼此可区分）：

1. 资产是否配置            -> P000
2. 熔断是否 OPEN（仅存款） -> C001（提取不受熔断限制，允许退出）
3. 是否有足够新鲜的有效估值 -> P006
4. 份额是否足够（仅提取）   -> L001

价格摄入 update_prices 永远返回 AggregationResult（不抛 P005），
同时把结果喂给熔断观察；不可用原因在结果里可解释地给出。
"""

import logging
import threading
import time

from .errors import (
    AssetNotConfiguredError,
    CircuitBreakerOpenError,
    PriceUnavailableError,
)
from .ledger import Ledger
from .models import (
    AggregationResult,
    AssetStatus,
    OperationReceipt,
    PriceQuote,
    VaultConfig,
)
from .price import PriceAggregator
from .risk import CircuitBreaker

logger = logging.getLogger("vault")


class Vault:
    def __init__(self, config: VaultConfig, clock=None):
        self.config = config
        self._clock = clock or time.time

        self._aggregators: dict[str, PriceAggregator] = {}
        self._breakers: dict[str, CircuitBreaker] = {}
        for symbol, acfg in config.assets.items():
            self._aggregators[symbol] = PriceAggregator(acfg, clock=self._clock)
            self._breakers[symbol] = CircuitBreaker(
                acfg, config.recovery_operators, clock=self._clock
            )

        precisions = {
            symbol: {"share": acfg.share_precision, "cash": acfg.cash_precision}
            for symbol, acfg in config.assets.items()
        }
        self.ledger = Ledger(precisions, clock=self._clock)

        # 每个资产一把串行锁，保证"判定+记账"原子，跨资产不互相阻塞。
        self._locks: dict[str, threading.RLock] = {
            symbol: threading.RLock() for symbol in config.assets
        }

    # ------------------------------------------------------------ 内部 -----

    def _require_symbol(self, symbol: str) -> None:
        if symbol not in self.config.assets:
            raise AssetNotConfiguredError(symbol)

    def _usable_price(self, symbol: str) -> AggregationResult:
        """返回当前可用于操作的聚合结果，否则按可区分原因拒绝。"""
        agg = self._aggregators[symbol]
        result = agg.last_result
        breaker = self._breakers[symbol]

        if result is None or not result.usable or result.price is None:
            raise PriceUnavailableError(
                symbol,
                "尚无成功聚合" if result is None else
                f"最近一次聚合不可用(code={result.failure_code})",
            )

        # 即使上次聚合成功，也要求其价格仍在保鲜年龄内，杜绝使用陈旧价。
        age = self._clock() - result.observed_at
        max_age = self.config.assets[symbol].max_price_age_seconds
        if age > max_age:
            # 陈旧估值本身也应让熔断观察到"不可用"，由摄入驱动长期不可用熔断；
            # 这里操作侧直接拒绝。
            raise PriceUnavailableError(
                symbol, f"上次有效估值已过期 age={age:.3f}s > max_age={max_age:.3f}s"
            )
        # breaker 已含在调用方检查；这里返回结果。
        _ = breaker
        return result

    # ------------------------------------------------------------ 摄入 -----

    def update_prices(self, symbol: str, quotes: list[PriceQuote]) -> AggregationResult:
        self._require_symbol(symbol)
        with self._locks[symbol]:
            result = self._aggregators[symbol].ingest(quotes)
            self._breakers[symbol].observe(result)
        return result

    # ------------------------------------------------------------ 存取 -----

    def deposit(self, user: str, symbol: str, shares) -> OperationReceipt:
        self._require_symbol(symbol)
        with self._locks[symbol]:
            breaker = self._breakers[symbol]
            if breaker.is_open:
                reasons = [t.detail for t in breaker.triggers] or ["熔断已触发"]
                logger.warning("[%s] 存款拒绝(C001): user=%s", symbol, user)
                raise CircuitBreakerOpenError(symbol, reasons)

            result = self._usable_price(symbol)  # -> P006
            price = result.price
            entry = self.ledger.deposit(user, symbol, shares, price)
            self.ledger.verify_conservation(symbol, price)
            return OperationReceipt(entry=entry, price=price,
                                    breaker_state=breaker.state)

    def withdraw(self, user: str, symbol: str, shares) -> OperationReceipt:
        self._require_symbol(symbol)
        with self._locks[symbol]:
            # 提取允许在熔断期间进行（允许退出敞口），但仍需有效价。
            result = self._usable_price(symbol)  # -> P006
            price = result.price
            entry = self.ledger.withdraw(user, symbol, shares, price)  # -> L001
            self.ledger.verify_conservation(symbol, price)
            return OperationReceipt(entry=entry, price=price,
                                    breaker_state=self._breakers[symbol].state)

    # ------------------------------------------------------------ 恢复 -----

    def recover(self, symbol: str, operator: str):
        self._require_symbol(symbol)
        with self._locks[symbol]:
            return self._breakers[symbol].recover(operator)

    # ------------------------------------------------------------ 状态 -----

    def breaker(self, symbol: str) -> CircuitBreaker:
        self._require_symbol(symbol)
        return self._breakers[symbol]

    def last_aggregation(self, symbol: str) -> AggregationResult | None:
        self._require_symbol(symbol)
        return self._aggregators[symbol].last_result

    def status(self, symbol: str) -> AssetStatus:
        self._require_symbol(symbol)
        result = self._aggregators[symbol].last_result
        br = self._breakers[symbol]
        last_price = result.price if result and result.usable else None
        observed = result.observed_at if result else None
        return AssetStatus(
            symbol=symbol,
            breaker=br.state,
            usable=bool(result and result.usable),
            last_price=last_price,
            last_observed_at=observed,
            triggers=tuple(br.triggers),
            total_shares=self.ledger.total_shares(symbol),
            net_cash=self.ledger.net_cash(symbol),
        )
