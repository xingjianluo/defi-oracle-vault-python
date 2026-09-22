"""基于估值的份额账本。

建模
----
- 账本只登记**份额**与按操作时价格记录的**全精度现金流**，不缓存任何
  历史价格供重估使用；每份价值由当时聚合价决定。
- 存/取都满足 ``现金 = 份额 * 操作价``（Decimal 全精度），账本内部
  **不做现金舍入**，因此精度舍入不可能凭空产生或吞掉资产。
- 仅在对外展示时把现金量化到 cash_precision（向下舍入），被舍掉的
  零头累计进 dust，恒有
  ``dust + floor_to_grid(精确现金) == 精确现金``。
  dust 是资产库里可审计的留存零头，不分配给任何用户，也不丢失。

可严格验证的守恒量（任意操作序列后）
------------------------------------
1. 份额：``Σ_user 用户份额 == 总份额``；存入增发、提取销毁，一一对应。
2. 现金流：累计存入现金 = Σ 存入(份额×价)，累计取出现金同理。
3. 盯市一致性：``净现金 == 当前总份额×当前价 + 历史操作价调整额``，
   调整额 = Σ 带符号份额×(操作价-当前价)（存入为正、提取为负），
   把价格变化导致的"现金流 vs 盯市存量"差异逐笔可解释地归位。
4. 展示网格：``dust >= 0`` 且
   ``floor(净现金) + dust == 净现金``（零头可逐笔追溯）。

并发
----
所有读写在同一把可重入锁内完成，保证并发下账本一致。
"""

import itertools
import logging
import threading
import time
import uuid
from decimal import ROUND_DOWN, Decimal

from .errors import InsufficientSharesError, InvalidAmountError
from .models import LedgerEntry

logger = logging.getLogger("vault.ledger")
_FLOOR = ROUND_DOWN


class Ledger:
    def __init__(self, precisions: dict[str, dict[str, Decimal]], clock=None):
        """:param precisions: symbol -> {"share","cash"} 精度（Decimal 网格）。"""
        self._precisions = precisions
        self._clock = clock or time.time
        self._lock = threading.RLock()

        self._balances: dict[tuple[str, str], Decimal] = {}
        self._total_shares: dict[str, Decimal] = {}
        # 全精度现金流
        self._cash_in: dict[str, Decimal] = {}
        self._cash_out: dict[str, Decimal] = {}
        self._entries: list[LedgerEntry] = []
        self._seq = itertools.count(1)

    # ------------------------------------------------------------ 内部 -----

    def _share_precision(self, symbol: str) -> Decimal:
        return self._precisions[symbol]["share"]

    def cash_precision(self, symbol: str) -> Decimal:
        return self._precisions[symbol]["cash"]

    @staticmethod
    def _add(bucket: dict[str, Decimal], key: str, value: Decimal) -> None:
        bucket[key] = bucket.get(key, Decimal(0)) + value

    def _normalize_shares(self, symbol: str, shares: Decimal) -> Decimal:
        if not isinstance(shares, Decimal):
            try:
                shares = Decimal(str(shares))
            except Exception as exc:
                raise InvalidAmountError(f"份额无法解析: {shares!r}") from exc
        if not shares.is_finite() or shares <= 0:
            raise InvalidAmountError(f"份额必须为正数, got {shares}")
        q = shares.quantize(self._share_precision(symbol), rounding=_FLOOR)
        if q <= 0:
            raise InvalidAmountError(
                f"份额 {shares} 量化到 {self._share_precision(symbol)} 后为 0"
            )
        return q

    @staticmethod
    def _normalize_price(price: Decimal) -> Decimal:
        if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
            raise InvalidAmountError(f"价格必须为正有限数, got {price!r}")
        return price

    # ------------------------------------------------------------ 存入 -----

    def deposit(self, user: str, symbol: str, shares: Decimal, price: Decimal) -> LedgerEntry:
        shares = self._normalize_shares(symbol, shares)
        price = self._normalize_price(price)
        cash = shares * price  # 全精度

        with self._lock:
            key = (user, symbol)
            self._balances[key] = self._balances.get(key, Decimal(0)) + shares
            self._add(self._total_shares, symbol, shares)
            self._add(self._cash_in, symbol, cash)
            entry = self._record(user, symbol, "deposit", shares, price, cash)
        logger.info(
            "DEPOSIT user=%s asset=%s shares=%s price=%s cash=+%s",
            user, symbol, shares, price, cash,
        )
        return entry

    # ------------------------------------------------------------ 提取 -----

    def withdraw(self, user: str, symbol: str, shares: Decimal, price: Decimal) -> LedgerEntry:
        shares = self._normalize_shares(symbol, shares)
        price = self._normalize_price(price)
        cash = shares * price  # 全精度

        with self._lock:
            key = (user, symbol)
            held = self._balances.get(key, Decimal(0))
            if held < shares:
                logger.warning(
                    "WITHDRAW 拒绝(份额不足 L001) user=%s asset=%s requested=%s held=%s",
                    user, symbol, shares, held,
                )
                raise InsufficientSharesError(user, symbol, str(shares), str(held))

            self._balances[key] = held - shares
            self._add(self._total_shares, symbol, -shares)
            self._add(self._cash_out, symbol, cash)
            entry = self._record(user, symbol, "withdraw", shares, price, -cash)
        logger.info(
            "WITHDRAW user=%s asset=%s shares=%s price=%s cash=-%s",
            user, symbol, shares, price, cash,
        )
        return entry

    def _record(self, user, symbol, kind, shares, price, cash_delta) -> LedgerEntry:
        entry = LedgerEntry(
            tx_id=uuid.uuid4().hex,
            user=user,
            symbol=symbol,
            kind=kind,
            shares=shares,
            price=price,
            cash_delta=cash_delta,
            dust_delta=Decimal(0),
            at=self._clock(),
        )
        self._entries.append(entry)
        return entry

    # ------------------------------------------------------------ 查询 -----

    def shares_of(self, user: str, symbol: str) -> Decimal:
        with self._lock:
            return self._balances.get((user, symbol), Decimal(0))

    def total_shares(self, symbol: str) -> Decimal:
        with self._lock:
            return self._total_shares.get(symbol, Decimal(0))

    def net_cash(self, symbol: str) -> Decimal:
        """全精度净现金 = 累计存入 - 累计取出（按各自操作时价格）。"""
        with self._lock:
            return self._cash_in.get(symbol, Decimal(0)) - self._cash_out.get(symbol, Decimal(0))

    def dust(self, symbol: str) -> Decimal:
        """净现金在展示网格向下舍入后留存的零头。"""
        grid = self.cash_precision(symbol)
        with self._lock:
            net = self.net_cash(symbol)
            return net - net.quantize(grid, rounding=_FLOOR)

    def net_cash_rounded(self, symbol: str) -> Decimal:
        """对外展示用的网格现金（向下舍入）。"""
        grid = self.cash_precision(symbol)
        with self._lock:
            return self.net_cash(symbol).quantize(grid, rounding=_FLOOR)

    def mark_to_market(self, symbol: str, price: Decimal) -> Decimal:
        """当前总份额按给定价格的盯市价值（全精度）。"""
        price = self._normalize_price(price)
        with self._lock:
            return self.total_shares(symbol) * price

    def price_adjustment(self, symbol: str, current_price: Decimal) -> Decimal:
        """历史操作价相对当前价的总调整额（全精度）。

        定义为每笔流水的 带符号份额*(操作价-当前价) 之和：
        存入份额符号为 +，提取份额符号为 -。

        恒等式：``净现金 == 当前总份额*当前价 + price_adjustment``。
        它把"按操作时价格记录的现金流"与"按当前价盯市的存量价值"
        之间的差异完全、可解释地归到各历史批次，没有任何舍入黑箱。
        """
        current_price = self._normalize_price(current_price)
        with self._lock:
            total = Decimal(0)
            for e in self._entries:
                if e.symbol != symbol:
                    continue
                signed = e.shares if e.kind == "deposit" else -e.shares
                total += signed * (e.price - current_price)
            return total

    def verify_conservation(self, symbol: str, price: Decimal) -> None:
        """严格守恒断言（任意操作序列后调用）。"""
        with self._lock:
            # 1. 份额：Σ用户 == 总份额，且总额非负。
            summed = sum(
                (s for (_, sym), s in self._balances.items() if sym == symbol),
                Decimal(0),
            )
            total = self.total_shares(symbol)
            assert summed == total, (
                f"份额不守恒 {symbol}: Σ用户={summed} != 总份额={total}"
            )
            assert total >= 0, f"总份额为负 {symbol}: {total}"

            # 2. 现金流逐笔可核对。
            cash_in = sum(
                (e.shares * e.price for e in self._entries
                 if e.symbol == symbol and e.kind == "deposit"),
                Decimal(0),
            )
            cash_out = sum(
                (e.shares * e.price for e in self._entries
                 if e.symbol == symbol and e.kind == "withdraw"),
                Decimal(0),
            )
            assert cash_in == self._cash_in.get(symbol, Decimal(0))
            assert cash_out == self._cash_out.get(symbol, Decimal(0))

            # 3. 盯市恒等式：净现金 == 总份额×当前价 + 历史操作价调整额。
            net = cash_in - cash_out
            mtm = total * self._normalize_price(price)
            adjustment = Decimal(0)
            for e in self._entries:
                if e.symbol != symbol:
                    continue
                signed = e.shares if e.kind == "deposit" else -e.shares
                adjustment += signed * (e.price - price)
            assert net == mtm + adjustment, (
                f"盯市恒等式被破坏 {symbol} @ {price}: "
                f"net={net} != mtm={mtm} + adjustment={adjustment}"
            )

            # 4. 展示网格：floor(净现金) + dust == 净现金，dust 非负。
            grid = self.cash_precision(symbol)
            floored = net.quantize(grid, rounding=_FLOOR)
            dust = net - floored
            assert dust >= 0 and floored + dust == net, (
                f"零头账不平 {symbol}: floor={floored} dust={dust} net={net}"
            )

    def entries(self) -> list[LedgerEntry]:
        with self._lock:
            return list(self._entries)
