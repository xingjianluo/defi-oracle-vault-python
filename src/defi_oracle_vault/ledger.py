"""本地份额账本。

守恒约定（任何操作序列后成立）：
    total_shares(asset) == sum(各用户份额) + dust(asset)

- 账本只记录份额；1 份额 = 1 单位资产，NAV 由服务层用聚合价格推导。
- 存入份额增加、提取份额扣减；不允许透支（InsufficientShares）。
- 份额采用定点小数（share_scale 位），非法精度直接拒绝（InvalidAmount），
  服务层的价值->份额换算残差以计价货币找零形式保留，不会进入或吞噬资产。
"""

from __future__ import annotations

import threading
from decimal import Decimal

from .errors import InsufficientShares, InvalidAmount, UnknownAsset

ZERO = Decimal(0)


class Ledger:
    """线程安全的内存账本。"""

    def __init__(self, share_scale: int = 18) -> None:
        self._balances: dict[str, dict[str, Decimal]] = {}
        self._dust: dict[str, Decimal] = {}
        self._lock = threading.RLock()
        self.share_scale = share_scale

    def register_asset(self, asset: str) -> None:
        """注册资产账本（幂等）。"""
        with self._lock:
            self._balances.setdefault(asset, {})
            self._dust.setdefault(asset, ZERO)

    def is_registered(self, asset: str) -> bool:
        with self._lock:
            return asset in self._balances

    def deposit(self, asset: str, user: str, shares: Decimal) -> Decimal:
        """存入份额，返回操作后该用户余额。"""
        shares = self._check_share_amount(shares)
        with self._lock:
            if asset not in self._balances:
                raise UnknownAsset(asset)
            balances = self._balances[asset]
            balances[user] = balances.get(user, ZERO) + shares
            return balances[user]

    def withdraw(self, asset: str, user: str, shares: Decimal) -> Decimal:
        """提取份额，余额不足抛 InsufficientShares；返回操作后余额。"""
        shares = self._check_share_amount(shares)
        with self._lock:
            if asset not in self._balances:
                raise UnknownAsset(asset)
            balances = self._balances[asset]
            current = balances.get(user, ZERO)
            if current < shares:
                raise InsufficientShares(user, str(shares), str(current))
            new_balance = current - shares
            if new_balance == 0:
                balances.pop(user, None)
            else:
                balances[user] = new_balance
            return new_balance

    def balance_of(self, asset: str, user: str) -> Decimal:
        """查询用户份额（未注册资产或用户返回 0）。"""
        with self._lock:
            if asset not in self._balances:
                return ZERO
            return self._balances[asset].get(user, ZERO)

    def total_shares(self, asset: str) -> Decimal:
        """份额总量（含尘埃）。"""
        with self._lock:
            if asset not in self._balances:
                return ZERO
            return sum(self._balances[asset].values(), ZERO) + self._dust[asset]

    def user_shares_sum(self, asset: str) -> Decimal:
        """所有用户份额之和。"""
        with self._lock:
            if asset not in self._balances:
                return ZERO
            return sum(self._balances[asset].values(), ZERO)

    def dust(self, asset: str) -> Decimal:
        with self._lock:
            return self._dust.get(asset, ZERO)

    def holders(self, asset: str) -> dict[str, Decimal]:
        """返回用户份额快照拷贝。"""
        with self._lock:
            if asset not in self._balances:
                return {}
            return dict(self._balances[asset])

    def _check_share_amount(self, shares: Decimal) -> Decimal:
        """校验份额为有限正数且精度合法（小数位不超过 share_scale）。"""
        if not isinstance(shares, Decimal):
            shares = Decimal(shares)
        if not shares.is_finite() or shares <= 0:
            raise InvalidAmount(f"份额必须为有限正数, 收到 {shares}")
        exponent = shares.as_tuple().exponent
        if isinstance(exponent, int) and exponent < -self.share_scale:
            raise InvalidAmount(
                f"份额精度超出 share_scale={self.share_scale}: {shares}"
            )
        return shares
